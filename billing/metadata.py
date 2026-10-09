from __future__ import annotations

from dataclasses import dataclass

from djstripe.models import Customer, ProductFeature, Subscription

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


def product_category(product) -> str | None:
    return (getattr(product, "metadata", None) or {}).get("category")


@dataclass
class ProductMetadata:
    stripe_id: str
    name: str | None = None
    features: list[str] | None = None
    description: str = ""
    monthly_upload_limit: int | None = None
    monthly_scan_limit: int | None = None
    metered: bool = False


VERITY_FREE = ProductMetadata(
    stripe_id="free",
    name="Free",
    description="For personal use",
    features=[
        features.RECORD_RETENTION,
        features.EXPORT_RECORDS,
        features.LIMITED_SCANS,
        features.SUPPORTING_FILE_UPLOAD,
        features.EXPIRY_REMINDERS,
        features.UPLOAD_ALLOWANCE,
    ],
    monthly_upload_limit=features.FREE_MONTHLY_UPLOAD_LIMIT,
    monthly_scan_limit=features.FREE_MONTHLY_SCAN_LIMIT,
)

VERITY_PRO = ProductMetadata(
    stripe_id="prod_VC8WUN1RO4Apqx",
    name="Pro",
    monthly_upload_limit=features.PRO_MONTHLY_UPLOAD_LIMIT,
    monthly_scan_limit=features.PRO_MONTHLY_SCAN_LIMIT,
)

USAGE_BASED_STORAGE = ProductMetadata(
    stripe_id="prod_VNkBxaDz3cZbza",
    name="Usage-Based Storage",
    metered=True,
)

PRODUCTS = {
    VERITY_PRO.stripe_id: VERITY_PRO,
    VERITY_FREE.stripe_id: VERITY_FREE,
    USAGE_BASED_STORAGE.stripe_id: USAGE_BASED_STORAGE,
}


def active_subscriptions(user) -> list:
    """The user's active subscriptions, across every customer linked to them."""
    if not getattr(user, "is_authenticated", False):
        return []

    cached = getattr(user, "_active_subs", None)
    if cached is not None:
        return cached

    customer_ids = set(Customer.objects.filter(subscriber=user).values_list("id", flat=True))
    own = getattr(user, "customer", None)
    if own is not None:
        customer_ids.add(own.id)

    subscriptions = (
        Subscription.objects.filter(customer_id__in=customer_ids)
        .prefetch_related("items__price__product")
        .order_by("pk")
        if customer_ids
        else []
    )
    user._active_subs = [s for s in subscriptions if is_active_subscription(s)]
    return user._active_subs


def live_items(subscription) -> list:
    """Held items, minus the rows dj-stripe leaves behind for items Stripe has removed."""
    payload = (subscription.stripe_data or {}).get("items")
    if payload is None:
        return [item for item in subscription.items.all() if item.price]
    live = {entry["price"]["id"] for entry in payload["data"] if entry.get("price", {}).get("id")}
    return [item for item in subscription.items.all() if item.price and item.price.id in live]


def held_products(user) -> dict[str, ProductMetadata]:
    """Products the user currently holds, keyed by Stripe product id."""
    return {
        meta.stripe_id: meta
        for subscription in active_subscriptions(user)
        for item in live_items(subscription)
        if (meta := PRODUCTS.get(item.price.product.id))
    }


def plan_for_user(user) -> ProductMetadata:
    """The user's plan, or Free. Metered add-ons are not plans."""
    return next((m for m in held_products(user).values() if not m.metered), VERITY_FREE)


def has_metered_storage(user) -> bool:
    return USAGE_BASED_STORAGE.stripe_id in held_products(user)


def is_active_subscription(subscription) -> bool:
    """True while the subscription grants access, including up to a scheduled cancel."""
    return getattr(subscription, "status", None) in ACTIVE_SUBSCRIPTION_STATUSES or bool(
        getattr(subscription, "cancel_at_period_end", False)
    )


def granted_features(user) -> set[str]:
    """lookup_keys of the Stripe features attached to the products "user" holds."""
    return {
        product_feature.entitlement_feature.stripe_data.get("lookup_key")
        for product_feature in ProductFeature.objects.filter(
            product_id__in=held_products(user)
        ).select_related("entitlement_feature")
    }
