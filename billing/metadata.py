from dataclasses import dataclass

from django.db.models import Prefetch
from djstripe.models import Customer, SubscriptionItem

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

_NOT_CACHED = object()


@dataclass
class ProductMetadata:
    """
    Metadata for a Stripe product.

    "category" groups products into pricing tables:
      * "base_plan"    -> the main plan that determines plan features (Free/Pro/...)
      * "storage_plan" -> add-on storage products that only raise the storage limit
    """

    stripe_id: str
    name: str
    features: list[str]

    @property
    def id(self) -> str:
        """Compatibility alias used by views and tests that treat metadata like Stripe objects."""
        return self.stripe_id

    description: str = ""
    category: str = "base_plan"
    storage_limit_gb: int = 0
    # Monthly Quick Scan allowance; None means unlimited. Storage add-ons leave
    # this unset -- scan entitlement always comes from the user's base plan.
    monthly_scan_limit: int | None = None
    # If True, this product is only available to users with a paid base plan.
    # Free users can see it as "disabled" but cannot purchase it via Stripe.
    pro_only: bool = False
    recommended: bool = False  # If True, this product is recommended for most users


VERITY_FREE = ProductMetadata(
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

VERITY_PRO = ProductMetadata(
    stripe_id="prod_VC8WUN1RO4Apqx",
    name="Verity Pro",
    description="For small businesses and teams",
    category="base_plan",
    features=[
        features.INCLUDES_ALL_FREE,
        features.UNLIMITED_SCANS,
        features.BANK_TRANSACTION_SYNC,
        features.QUICK_REIMBURSEMENT_REQUEST,
        features.PRO_STORAGE_LIMIT,
        features.AUTO_TXN_CATEGORIZATION,
        features.RECORD_SHARING,
    ],
    storage_limit_gb=features.PRO_STORAGE_LIMIT_GB,
    monthly_scan_limit=features.PRO_SCAN_LIMIT,
    recommended=True,  # Recommended plan for most users
)

STORAGE_UPGRADE_10 = ProductMetadata(
    stripe_id="prod_V0dPTSMZjCZNuk",
    name="10 GB Storage Pack",
    description="Additional 10 GB cloud storage (Pro users only)",
    category="storage_plan",
    features=[
        features.STORAGE_UPGRADE_GB_10,
    ],
    storage_limit_gb=features.STORAGE_ADDITIONAL_GB_10,
    pro_only=True,  # Pro users only
)

STORAGE_UPGRADE_5 = ProductMetadata(
    stripe_id="prod_VCgHfYDCC6aDCN",
    name="5 GB Storage Pack",
    description="Additional 5 GB cloud storage (Pro users only)",
    category="storage_plan",
    features=[
        features.STORAGE_UPGRADE_GB_5,
    ],
    storage_limit_gb=features.STORAGE_ADDITIONAL_GB_5,
    pro_only=True,  # Pro users only
)

STORAGE_UPGRADE_1 = ProductMetadata(
    stripe_id="prod_VCg5fBU3aujCi9",
    name="1 GB Storage Pack",
    description="Additional 1 GB cloud storage (available to all plans)",
    category="storage_plan",
    features=[
        features.STORAGE_UPGRADE_GB_1,
    ],
    storage_limit_gb=features.STORAGE_ADDITIONAL_GB_1,
    pro_only=False,  # Available to everyone (free or paid)
)

PRODUCTS = {
    VERITY_PRO.stripe_id: VERITY_PRO,
    VERITY_FREE.stripe_id: VERITY_FREE,
    STORAGE_UPGRADE_10.stripe_id: STORAGE_UPGRADE_10,
    STORAGE_UPGRADE_5.stripe_id: STORAGE_UPGRADE_5,
    STORAGE_UPGRADE_1.stripe_id: STORAGE_UPGRADE_1,
}


def category_for_product(product_id: str | None) -> str | None:
    """Return the pricing category for a Stripe product ID, or None if unknown."""
    if product_id is None:
        return None
    meta = PRODUCTS.get(product_id)
    return meta.category if meta is not None else None


def _active_subscriptions(user):
    """Return the user's active subscriptions, from the customer plus the direct FK.

    djstripe stores the subscription payload in "stripe_data" and exposes
    "status" as a derived property, so status is filtered in Python.

    Subscriptions are collected from both the user's "customer" FK and any
    Stripe Customer whose "subscriber" points at this user. The two can
    diverge when a legacy customer (created without "subscriber") predates
    "Customer.get_or_create": a later checkout then creates a fresh customer
    and its subscriptions would otherwise be invisible on the dashboard.

    The result is memoized on the user instance so a single request (which
    shares one "request.user" object across context processors and views)
    runs the queries at most once.
    """
    cached = getattr(user, "_pt_active_subscriptions", _NOT_CACHED)
    if cached is not _NOT_CACHED:
        return cached
    if not getattr(user, "is_authenticated", False):
        return []

    subscriptions = []
    items_prefetch = Prefetch(
        "items",
        queryset=SubscriptionItem.objects.select_related("price", "price__product"),
    )
    customer = getattr(user, "customer", None)
    if customer is not None:
        subscriptions.extend(customer.subscriptions.prefetch_related(items_prefetch).all())

    linked_customers = Customer.objects.filter(subscriber=user).exclude(
        pk=customer.pk if customer is not None else None
    )
    for linked in linked_customers:
        subscriptions.extend(linked.subscriptions.prefetch_related(items_prefetch).all())

    direct = getattr(user, "subscription", None)
    if direct is not None and not any(s.pk == direct.pk for s in subscriptions):
        subscriptions.append(direct)
    active = []
    for sub in subscriptions:
        status = getattr(sub, "status", None)
        cancel_at_period_end = bool(getattr(sub, "cancel_at_period_end", False))
        if status in ACTIVE_SUBSCRIPTION_STATUSES or cancel_at_period_end:
            active.append(sub)
    user._pt_active_subscriptions = active
    return active


def _metas_for_subscription(subscription):
    """Yield ProductMetadata for each item on a single subscription.

    Items are prefetched together with their price and product by
    "_active_subscriptions" (via "Prefetch"), so this is served from the
    query cache rather than issuing a query per subscription.
    """
    for item in subscription.items.all():
        product = item.price.product if item.price is not None else None
        meta = PRODUCTS.get(product.id) if product is not None else None
        if meta is not None:
            yield meta


def _products_by_category(user) -> dict[str, ProductMetadata]:
    """Return at most one ProductMetadata per category for the user.

    Exactly one plan per category is allowed. When several active
    subscriptions cover the same category (an old plan whose Stripe
    cancellation hasn't landed yet, a webhook-synced duplicate, ...), the
    most recently created subscription wins so a new purchase "replaces"
    rather than stacks with the previous one.
    """
    entries = [
        (subscription.created, subscription.pk, meta)
        for subscription in _active_subscriptions(user)
        for meta in _metas_for_subscription(subscription)
    ]
    winners: dict[str, ProductMetadata] = {}
    for _created, _pk, meta in sorted(entries, key=lambda e: (e[0], e[1]), reverse=True):
        winners.setdefault(meta.category, meta)
    return winners


def plan_for_user(user) -> ProductMetadata:
    """Return the user's base plan (drives plan features), or Free if none."""
    return _products_by_category(user).get("base_plan", VERITY_FREE)


def storage_addons_for_user(user) -> list[ProductMetadata]:
    """Return the user's valid storage add-on product(s), if any.

    Pro-only packs require an active paid base plan. General storage add-ons
    may still apply to free users, but a base plan cancellation removes the
    Pro-only entitlement without invalidating the entire storage add-on model.
    """
    addon = _products_by_category(user).get("storage_plan")
    if addon is None:
        return []
    if plan_for_user(user).stripe_id == VERITY_FREE.stripe_id and addon.pro_only:
        return []
    return [addon]


CATEGORY_ORDER = {"base_plan": 0, "storage_plan": 1}


def active_products_for_user(user) -> list[ProductMetadata]:
    """Return metadata for every product the user is actively subscribed to.

    At most one product per category, base plan first, so callers can render
    plan names without hardcoding anything.
    """
    products = list(_products_by_category(user).values())
    return sorted(products, key=lambda p: (CATEGORY_ORDER.get(p.category, 99), p.name))
