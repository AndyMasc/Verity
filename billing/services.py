"""Service layer for Stripe billing operations."""

import logging
from copy import copy

import stripe
from django.db.models import F
from django.http import HttpRequest, HttpResponseBadRequest
from djstripe.models import Price, Product
from djstripe.settings import djstripe_settings

from . import metadata

logger = logging.getLogger(__name__)


def _configure() -> None:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY


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
    for price_id in price_ids:
        if price_id in metas:
            (metered if metas[price_id].metered else licensed).append(price_id)

    # A licensed line item must carry a quantity; a metered one must not.
    line_items = [{"price": price_id, "quantity": quantity} for price_id in licensed]
    line_items += [{"price": price_id} for price_id in metered]

    if not line_items:
        return HttpResponseBadRequest("Select a valid plan.")

    return line_items


def sanitize_line_items(subscription, line_items: list[dict]) -> list[dict]:
    held_items = metadata.live_items(subscription) if subscription else []

    held_by_category: dict[str, list] = {}
    for held_item in held_items:
        if not held_item.price:
            continue
        category = metadata.product_category(held_item.price.product)
        if category:
            held_by_category.setdefault(category, []).append(held_item)

    incoming_price_ids = {item["price"] for item in line_items}
    incoming_price_categories = {
        price.id: metadata.product_category(price.product)
        for price in Price.objects.filter(id__in=incoming_price_ids).select_related("product")
    }

    sanitized_line_items = []
    emitted_ids: set[str] = set()
    for item in line_items:
        price = item["price"]
        category_items = held_by_category.get(incoming_price_categories.get(price), [])

        keep = next(
            (
                held
                for held in category_items
                if held.price.id == price and held.id not in emitted_ids
            ),
            None,
        )
        if keep is None:
            sanitized_line_items.append({"price": price})
        else:
            emitted_ids.add(keep.id)
            sanitized_line_items.append({"id": keep.id})

        for held_item in category_items:
            if held_item.id in emitted_ids:
                continue
            emitted_ids.add(held_item.id)
            sanitized_line_items.append({"id": held_item.id, "deleted": True})

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
