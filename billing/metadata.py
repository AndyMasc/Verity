from dataclasses import dataclass

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})


@dataclass
class ProductMetadata:
    """
    Metadata for a Stripe product.
    """

    stripe_id: str
    name: str
    features: list[str]
    description: str = ""
    is_default: bool = False
    storage_limit_gb: int = 0


PAPERTRAIL_FREE = ProductMetadata(
    stripe_id="free",
    name="Free",
    description="For personal use",
    features=[
        features.LIMITED_SCANS,
        features.SUPPORTING_FILE_UPLOAD,
        features.EXPIRY_REMINDERS,
        features.FREE_STORAGE_LIMIT,
    ],
    storage_limit_gb=features.FREE_STORAGE_LIMIT_GB,
)

PAPERTRAIL_PRO = ProductMetadata(
    stripe_id="prod_UynmT2hCmUF08u",
    name="Papertrail Pro",
    description="For small businesses and teams",
    is_default=True,
    features=[
        features.INCLUDES_ALL_FREE,
        features.UNLIMITED_SCANS,
        features.BANK_TRANSACTION_SYNC,
        features.QUICK_REIMBURSEMENT_REQUEST,
        features.PRO_STORAGE_LIMIT,
    ],
    storage_limit_gb=features.PRO_STORAGE_LIMIT_GB,
)

PRODUCTS = {
    PAPERTRAIL_PRO.stripe_id: PAPERTRAIL_PRO,
    PAPERTRAIL_FREE.stripe_id: PAPERTRAIL_FREE,
}


def plan_for_subscription(subscription) -> ProductMetadata:
    """Return the ProductMetadata matching the subscription's plan, else the Pro default.

    The subscription's plan is resolved from its subscription items (Stripe
    product linked through Price). Falls back to the Pro metadata so an active
    subscriber whose product is unknown still gets paid-plan limits.
    """
    if subscription is not None:
        items = subscription.items.select_related("price", "price__product").all()
        for item in items:
            product_id = item.price.product_id if item.price is not None else None
            meta = PRODUCTS.get(product_id)
            if meta is not None:
                return meta
    return PAPERTRAIL_PRO


def plan_for_user(user) -> ProductMetadata:
    """Return the ProductMetadata for the user's current plan.

    An active subscription resolves to its own plan metadata; otherwise the
    user is on the Free plan.
    """
    subscription = getattr(user, "subscription", None)
    if subscription is not None and subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
        return plan_for_subscription(subscription)
    return PAPERTRAIL_FREE
