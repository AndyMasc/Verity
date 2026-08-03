from dataclasses import dataclass

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


@dataclass
class ProductMetadata:
    """
    Metadata for a Stripe product.

    ``category`` groups products into pricing tables:
      * "base_plan"    -> the main plan that determines plan features (Free/Pro/...)
      * "storage_plan" -> add-on storage products that only raise the storage limit
    """

    stripe_id: str
    name: str
    features: list[str]
    description: str = ""
    is_default: bool = False
    category: str = "base_plan"
    storage_limit_gb: int = 0
    # Monthly Quick Scan allowance; None means unlimited. Storage add-ons leave
    # this unset -- scan entitlement always comes from the user's base plan.
    monthly_scan_limit: int | None = None


PAPERTRAIL_FREE = ProductMetadata(
    stripe_id="free",
    name="Free",
    description="For personal use",
    category="base_plan",
    features=[
        features.LIMITED_SCANS,
        features.SUPPORTING_FILE_UPLOAD,
        features.EXPIRY_REMINDERS,
        features.FREE_STORAGE_LIMIT,
    ],
    storage_limit_gb=features.FREE_STORAGE_LIMIT_GB,
    monthly_scan_limit=features.FREE_MONTHLY_SCAN_LIMIT,
)

PAPERTRAIL_PRO = ProductMetadata(
    stripe_id="prod_V0BRybbfIkmRH4",
    name="Papertrail Pro",
    description="For small businesses and teams",
    is_default=True,
    category="base_plan",
    features=[
        features.INCLUDES_ALL_FREE,
        features.UNLIMITED_SCANS,
        features.BANK_TRANSACTION_SYNC,
        features.QUICK_REIMBURSEMENT_REQUEST,
        features.PRO_STORAGE_LIMIT,
    ],
    storage_limit_gb=features.PRO_STORAGE_LIMIT_GB,
)

STORAGE_UPGRADE_25 = ProductMetadata(
    stripe_id="prod_V0BMnncJMOVWoW",
    name="25GB Storage Upgrade",
    description="",
    is_default=False,
    category="storage_plan",
    features=[
        features.STORAGE_UPGRADE_25,
    ],
    storage_limit_gb=features.STORAGE_ADDITIONAL_GB,
)

PRODUCTS = {
    PAPERTRAIL_PRO.stripe_id: PAPERTRAIL_PRO,
    PAPERTRAIL_FREE.stripe_id: PAPERTRAIL_FREE,
    STORAGE_UPGRADE_25.stripe_id: STORAGE_UPGRADE_25,
}


def _active_subscriptions(user):
    """Return the user's active subscriptions, from the customer plus the direct FK.

    djstripe stores the subscription payload in ``stripe_data`` and exposes
    ``status`` as a derived property, so status is filtered in Python.
    """
    subscriptions = []
    customer = getattr(user, "customer", None)
    if customer is not None:
        subscriptions.extend(customer.subscriptions.all())
    direct = getattr(user, "subscription", None)
    if direct is not None and not any(s.pk == direct.pk for s in subscriptions):
        subscriptions.append(direct)
    return [
        sub for sub in subscriptions if getattr(sub, "status", None) in ACTIVE_SUBSCRIPTION_STATUSES
    ]


def _product_metas(subscriptions):
    """Yield ProductMetadata for each item across the given subscriptions."""
    for subscription in subscriptions:
        items = subscription.items.select_related("price", "price__product").all()
        for item in items:
            product_id = item.price.product_id if item.price is not None else None
            meta = PRODUCTS.get(product_id)
            if meta is not None:
                yield meta


def plan_for_subscription(subscription) -> ProductMetadata:
    """Return the base-plan metadata for a single subscription, or Free if none."""
    if subscription is not None:
        for meta in _product_metas([subscription]):
            if meta.category == "base_plan":
                return meta
    return PAPERTRAIL_FREE


def plan_for_user(user) -> ProductMetadata:
    """Return the user's base plan (drives plan features), or Free if none."""
    for meta in _product_metas(_active_subscriptions(user)):
        if meta.category == "base_plan":
            return meta
    return PAPERTRAIL_FREE


def storage_addons_for_user(user) -> list[ProductMetadata]:
    """Return the storage add-on products active for the user (each adds storage)."""
    return [
        meta
        for meta in _product_metas(_active_subscriptions(user))
        if meta.category == "storage_plan"
    ]


def active_products_for_user(user) -> list[ProductMetadata]:
    """Return metadata for every product the user is actively subscribed to.

    Includes the base plan and any storage add-ons, deduplicated by product,
    so callers can render plan names without hardcoding anything.
    """
    result: list[ProductMetadata] = []
    seen: set[str] = set()
    for meta in _product_metas(_active_subscriptions(user)):
        if meta.stripe_id not in seen:
            seen.add(meta.stripe_id)
            result.append(meta)
    return result
