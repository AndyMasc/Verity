"""Service layer for Stripe billing operations."""

import logging
from copy import copy

import stripe
from django.db.models import Count, F
from django.http import HttpRequest, HttpResponseBadRequest
from djstripe.models import Customer, Price, Product
from djstripe.settings import djstripe_settings

from . import metadata

logger = logging.getLogger(__name__)


def _configure() -> None:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY


def resolve_customer(user):
    linked = list(Customer.objects.filter(subscriber=user).annotate(subs=Count("subscriptions")))
    if not linked:
        return None

    keep = next((c for c in linked if c.pk == user.customer_id), None) or max(
        linked, key=lambda c: (c.subs, c.pk)
    )
    for cust in linked:
        if cust.pk != keep.pk:
            cust.subscriber = None
            cust.save(update_fields=["subscriber"])
    return keep


def fetch_line_items(
    request: HttpRequest, quantity: int = 1
) -> list[dict] | HttpResponseBadRequest:
    """Fetch Price objects for the selected price IDs in the request."""
    price_ids = request.POST.getlist("price_ids")
    products = Price.objects.filter(
        id__in=price_ids,
        active=True,
        livemode=getattr(djstripe_settings, "STRIPE_LIVE_MODE", False),
    ).select_related("product")

    categories = {metadata.product_category(price.product) for price in products}
    if not categories or len(categories) != len(products):
        return HttpResponseBadRequest(
            "Select a valid plan, or, deselect duplicate products from the same category."
        )
    metas = {
        price.id: ProductMetadata
        for price in products
        if (ProductMetadata := metadata.PRODUCTS.get(price.product.id))
    }

    licensed, metered = [], []
    for price_id in dict.fromkeys(price_ids):
        if price_id in metas:
            (metered if metas[price_id].metered else licensed).append(price_id)

    # A licensed line item must carry a quantity; a metered one must not.
    line_items = [{"price": price_id, "quantity": quantity} for price_id in licensed]
    line_items += [{"price": price_id} for price_id in metered]

    if not line_items:
        return HttpResponseBadRequest("Select a valid plan.")

    return line_items


def sanitize_line_items(subscription, line_items: list[dict]) -> list[dict]:
    """Clear stale line items from the subscription and return a list of items to send to Stripe."""
    held_items = metadata.live_items(subscription) if subscription else []

    category_to_held_items: dict[str, list] = {}
    for item in held_items:
        if not item.price:
            continue
        category = metadata.product_category(item.price.product)
        if category:
            category_to_held_items.setdefault(category, []).append(item)

    held_price_ids = {item.price.id for item in held_items if item.price}

    incoming_price_ids = {item["price"] for item in line_items}
    incoming_price_categories = {
        price.id: metadata.product_category(price.product)
        for price in Price.objects.filter(id__in=incoming_price_ids).select_related("product")
    }

    sanitized_line_items = []
    for item in line_items:
        price = item["price"]
        category = incoming_price_categories.get(price)

        for held_item in category_to_held_items.get(category, []):
            if held_item.price.id == price:
                sanitized_line_items.append({"id": held_item.id})
            else:
                sanitized_line_items.append({"id": held_item.id, "deleted": True})

        if price not in held_price_ids:
            sanitized_line_items.append({"price": price})

    return sanitized_line_items


def pricing_context(user) -> dict:
    """Build the pricing data shared by the pricing page and the landing page."""
    live = djstripe_settings.STRIPE_LIVE_MODE
    catalog = list(Product.objects.filter(active=True, livemode=live))

    prices = Price.objects.filter(product__in=catalog, active=True, livemode=live).order_by(
        F("djstripe_created").desc(nulls_last=True), "-djstripe_id"
    )

    checkout_price: dict[str, Price] = {}
    for price in prices:
        if (price.stripe_data or {}).get("recurring", {}).get("interval") == "month":
            checkout_price.setdefault(price.product_id, price)

    free = copy(metadata.VERITY_FREE)
    free.features_list = list(free.features)

    held_ids = set(metadata.held_products(user))

    for product in catalog:
        product.checkout_price = checkout_price.get(product.id)
        product.checkout_price_id = getattr(product.checkout_price, "id", None)
        product.already_active = product.id in held_ids

    products = [free, *catalog]

    return {
        "products": products,
        "has_active_subscription": bool(getattr(user, "has_active_subscription", False)),
    }
