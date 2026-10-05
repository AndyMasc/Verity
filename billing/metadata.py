from dataclasses import dataclass, field

from django.db.models import Prefetch
from djstripe.models import Customer, SubscriptionItem

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

_NOT_CACHED = object()


def product_category(product) -> str | None:
    """Return a Stripe product's pricing category.

    Stripe's product metadata is the single source of truth for categories, so
    that adding a plan needs no code change here. "base_plan" is the paid plan
    that drives features; "storage_plan" is usage-based storage.
    """
    return (getattr(product, "metadata", None) or {}).get("category")


@dataclass
class ProductMetadata:
    """
    Metadata for a Stripe product.
    """

    stripe_id: str
    name: str
    features: list[str]

    @property
    def id(self) -> str:
        """Compatibility alias used by views and tests that treat metadata like Stripe objects."""
        return self.stripe_id

    description: str = ""
    storage_limit_gb: float = 0
    monthly_scan_limit: int | None = None
    recommended: bool = False  # If True, this product is recommended for most users
    metered: bool = False
    price_ids: dict = field(default_factory=dict)

    @property
    def is_paid(self) -> bool:
        """True for a paid base plan. Only meaningful on base-plan products."""
        return self.stripe_id != VERITY_FREE.stripe_id

    def price_id_for_plan(self, paid_base: bool) -> str | None:
        """This metered product's price for a free or paid base plan.

        A metered product carries one price per plan so the included allowance
        can differ; the caller says which plan the order is actually buying.
        """
        if not self.metered:
            return None
        return self.price_ids.get("paid" if paid_base else "free")


VERITY_FREE = ProductMetadata(
    stripe_id="free",
    name="Free",
    description="For personal use",
    features=[
        features.RECORD_RETENTION,
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
    recommended=True,
)

USAGE_BASED_STORAGE = ProductMetadata(
    stripe_id="prod_VNkBxaDz3cZbza",
    name="Usage-Based Storage",
    description="For users who need more storage than the base plan allows",
    # Lifts the plan quota rather than raising it; the meter bills the difference.
    storage_limit_gb=0,
    metered=True,
    # One price per plan, so the included allowance differs: FREE_PLAN gets a 500 MB $0 tier, PAID_PLAN a 5000 MB one. Check Stripe - its the source of truth.
    price_ids={
        "free": "price_1UN6d3BR3saQICXCgJHfZd7O",
        "paid": "price_1UN6e5BR3saQICXCgQ5ViNNZ",
    },
    features=[features.USAGE_BASED_BILLING, features.NO_STORAGE_LIMIT, features.PRICE],
)

PRODUCTS = {
    VERITY_PRO.stripe_id: VERITY_PRO,
    VERITY_FREE.stripe_id: VERITY_FREE,
    USAGE_BASED_STORAGE.stripe_id: USAGE_BASED_STORAGE,
}


def _active_subscriptions(user):
    """Return the user's active subscriptions, from the customer plus the direct FK.

    djstripe stores the subscription payload in "stripe_data" and exposes
    "status" as a derived property, so status is filtered in Python.

    Subscriptions are collected from both the user's "customer" FK and any
    Stripe Customer whose "subscriber" points at this user.
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


def metas_for_subscription(subscription):
    """Yield ProductMetadata for each item on a single subscription."""
    for item in subscription.items.all():
        product = item.price.product if item.price is not None else None
        meta = PRODUCTS.get(product.id) if product is not None else None
        if meta is not None:
            yield meta


def _active_products(user) -> dict[str, ProductMetadata]:
    """Return the products the user is actively subscribed to, keyed by product id.

    A product can appear on two subscriptions at once (the old one whose
    cancellation has not landed yet, a webhook-synced duplicate), so the most
    recently created subscription wins rather than the product stacking.
    """
    entries = [
        (subscription.created, subscription.pk, meta)
        for subscription in _active_subscriptions(user)
        for meta in metas_for_subscription(subscription)
    ]
    winners: dict[str, ProductMetadata] = {}
    for _created, _pk, meta in sorted(entries, key=lambda e: (e[0], e[1]), reverse=True):
        winners.setdefault(meta.stripe_id, meta)
    return winners


def plan_for_user(user) -> ProductMetadata:
    """Return the user's plan (drives plan features), or Free if they have none.

    Usage-based storage is skipped: it is priced like a base plan but carries no
    plan features, so returning it would hand a free user the paid feature set.
    """
    return next(
        (meta for meta in _active_products(user).values() if not meta.metered),
        VERITY_FREE,
    )


def active_products_for_user(user) -> list[ProductMetadata]:
    """Return every product the user holds, their plan first.

    The plan comes first so callers can render the plan name without hardcoding
    an index.
    """
    return sorted(
        _active_products(user).values(),
        key=lambda meta: (meta.metered, meta.name),
    )


def has_metered_storage(user) -> bool:
    """True when the user has subscribed to usage-based storage.

    Answered from the cached local subscription rather than Stripe, because this
    runs on every upload validation. Subscribing is what lifts the storage cap;
    the plan's own quota still drives what the user is billed for.
    """
    product_id = USAGE_BASED_STORAGE.stripe_id
    return any(
        item.price is not None and item.price.product_id == product_id
        for subscription in _active_subscriptions(user)
        for item in subscription.items.all()
    )


def metered_price_id(product: ProductMetadata, user) -> str | None:
    """Return the Stripe price id for "product" as it applies to "user".

    One lookup point for metered pricing: a product declares which plans it
    serves, and the caller's plan picks the price.
    """
    if not product.metered:
        return None
    return product.price_id_for_plan(plan_for_user(user).stripe_id != VERITY_FREE.stripe_id)
