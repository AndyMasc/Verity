"""Service layer for Stripe billing operations.

Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import logging

import stripe
from djstripe.models import Product, Subscription
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
) -> stripe.checkout.Session:
    """Create a Stripe subscription checkout session."""
    _configure()
    return stripe.checkout.Session.create(
        customer=customer,
        line_items=line_items,
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
    )


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


def pricing_context(user) -> dict:
    """Build the pricing data shared by the pricing page and the landing page."""
    products = list(Product.objects.filter(active=True).prefetch_related("prices"))
    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []

    free_plan = metadata.PAPERTRAIL_FREE
    free_plan.features_list = free_plan.features
    free_plan.prices = []
    free_plan.metadata = {"category": "base_plan"}
    products.insert(0, free_plan)

    def _by_category(category: str) -> list:
        return [
            p
            for p in products
            if isinstance(getattr(p, "metadata", None), dict)
            and p.metadata.get("category") == category
        ]

    return {
        "products": products,
        "free_plan": free_plan,
        "has_active_subscription": bool(user.is_authenticated and user.has_active_subscription),
        "base_plans": _by_category("base_plan"),
        "storage_plans": _by_category("storage_plan"),
    }


def reconcile_subscription_statuses(
    subscription_ids: list[str] | None = None,
) -> int:
    """Reconcile local djstripe subscription statuses against Stripe.

    Local rows drift from Stripe when a webhook event is missed (endpoint
    downtime, exhausted Stripe retries, a crashed handler, ...). This refetches
    each local subscription from Stripe and updates its stored status to match,
    so "plan_for_user" stops trusting stale "active" rows.

    Subscriptions that no longer exist in Stripe are marked "canceled".

    Args:
        subscription_ids: Optional filter; when None, all local subscriptions
            are reconciled.

    Returns:
        The number of local rows whose stored status was corrected.
    """
    _configure()

    queryset = Subscription.objects.all().order_by("id")
    if subscription_ids:
        queryset = queryset.filter(id__in=subscription_ids)

    corrected = 0
    for local in queryset.iterator(chunk_size=100):
        try:
            remote = stripe.Subscription.retrieve(local.id)
            remote_status = remote.status
        except stripe.error.InvalidRequestError:
            # No longer exists in Stripe (deleted/expired). The period ended
            # or it was removed out-of-band; local status is stale.
            remote_status = "canceled"
        except stripe.error.StripeError as exc:
            logger.warning(
                "Reconciliation: failed to fetch subscription %s from Stripe: %s",
                local.id,
                exc,
            )
            continue

        local_status = (local.stripe_data or {}).get("status")
        if local_status == remote_status:
            continue

        data = dict(local.stripe_data or {})
        data["status"] = remote_status
        Subscription.objects.filter(pk=local.pk).update(stripe_data=data)
        corrected += 1
        logger.info(
            "Reconciliation: %s status %s -> %s",
            local.id,
            local_status,
            remote_status,
        )

    return corrected
