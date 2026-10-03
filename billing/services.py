"""Service layer for Stripe billing operations.

Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import logging
from copy import copy

import stripe
from django.db.models import Prefetch
from djstripe.models import Price, Product
from djstripe.settings import djstripe_settings

from . import metadata

logger = logging.getLogger(__name__)


def _configure() -> None:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY


def retrieve_subscription(subscription_id: str) -> dict:
    """Fetch the latest Stripe subscription payload for the given ID."""
    _configure()
    return stripe.Subscription.retrieve(str(subscription_id))


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    """Fetch a Stripe checkout Session payload for the given ID."""
    _configure()
    return stripe.checkout.Session.retrieve(session_id)


def create_checkout_session(
    *,
    customer: str,
    line_items: list[dict],
    success_url: str,
    cancel_url: str,
    client_reference_id: str | None = None,
    idempotency_key: str | None = None,
) -> stripe.checkout.Session:
    """Create a Stripe subscription checkout session."""
    _configure()
    kwargs = {
        "customer": customer,
        "line_items": line_items,
        "mode": "subscription",
        "success_url": success_url,
        "cancel_url": cancel_url,
    }
    # Identifies the buyer on the session so webhooks can attribute the event
    # back to a user even when the customer row is not linked yet.
    if client_reference_id is not None:
        kwargs["client_reference_id"] = client_reference_id
    if idempotency_key is not None:
        kwargs["idempotency_key"] = idempotency_key
    return stripe.checkout.Session.create(**kwargs)


def create_billing_portal_session(
    *, customer: str, return_url: str
) -> stripe.billing_portal.Session:
    """Create a Stripe billing portal session."""
    _configure()
    return stripe.billing_portal.Session.create(customer=customer, return_url=return_url)


def cancel_subscription(subscription_id: str) -> None:
    """Cancel a Stripe subscription, logging failures for the caller."""
    _configure()
    stripe.Subscription.cancel(subscription_id)


def retrieve_customer(customer_id: str):
    """Return the Stripe customer, or None when it no longer exists."""
    _configure()
    try:
        return stripe.Customer.retrieve(customer_id)
    except stripe.error.InvalidRequestError:
        return None


def customer_missing_in_stripe(customer_id: str) -> bool:
    """Return True when a stored customer ID no longer exists in Stripe."""
    if not customer_id:
        return True
    remote = retrieve_customer(customer_id)
    if remote is None:
        return True
    return bool(getattr(remote, "deleted", False))


def _checkout_price_id(product: Product) -> str | None:
    """Return the price id to submit at checkout for a product, or None.

    Mirrors the pricing card display: the most recently created active,
    monthly-recurring price. "prices.first" is deliberately avoided because
    dj-stripe Prices have no default ordering, so it can select an archived
    price that the checkout validation (_validated_price) rejects.
    """
    candidates = [
        price
        for price in product.prices.all()
        if price.active and price.recurring and price.recurring.get("interval") == "month"
    ]
    if not candidates:
        return None
    newest = max(candidates, key=lambda price: price.djstripe_created)
    return newest.id


def _product_pro_only(meta, base_plan) -> bool:
    """Return whether a product should be hidden from users on the free plan."""
    if not meta or not meta.pro_only:
        return False
    return base_plan.stripe_id == metadata.VERITY_FREE.stripe_id


def _decorate_product_for_pricing(product, *, base_plan, held_product_ids):
    """Attach display metadata used by the pricing cards and checkout UI."""
    meta = metadata.PRODUCTS.get(product.id)
    product.category = meta.category if meta else None
    product.features_list = meta.features if meta else []
    product.checkout_price_id = _checkout_price_id(product)
    product.already_active = product.id in held_product_ids
    product.recommended = meta.recommended if meta else False
    product.pro_only = _product_pro_only(meta, base_plan)


def pricing_context(user) -> dict:
    """Build the pricing data shared by the pricing page and the landing page."""
    live = djstripe_settings.STRIPE_LIVE_MODE
    products = list(
        Product.objects.filter(active=True, livemode=live).prefetch_related(
            Prefetch("prices", queryset=Price.objects.filter(active=True, livemode=live))
        )
    )
    base_plan = metadata.plan_for_user(user)
    held_product_ids = {meta.stripe_id for meta in metadata.active_products_for_user(user)}

    for product in products:
        _decorate_product_for_pricing(
            product, base_plan=base_plan, held_product_ids=held_product_ids
        )

    # A copy, not the module-level constant: the free plan has no Stripe
    # product behind it but the cards expect the same shape.
    free_plan = copy(metadata.VERITY_FREE)
    free_plan.features_list = free_plan.features
    free_plan.prices = []
    free_plan.checkout_price_id = None
    free_plan.already_active = True
    products.insert(0, free_plan)

    return {
        "products": products,
        "free_plan": free_plan,
        "has_active_subscription": bool(user.is_authenticated and user.has_active_subscription),
        "base_plans": [p for p in products if p.category == "base_plan"],
        "storage_plans": [p for p in products if p.category == "storage_plan"],
    }
