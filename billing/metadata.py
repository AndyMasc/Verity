from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from django.db.models import Prefetch
from djstripe.models import Customer, Subscription, SubscriptionItem

from . import features

ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"active", "trialing"})

# Must match the "category" key on each Stripe product's metadata.
BASE_PLAN_CATEGORY = "base_plan"
STORAGE_PLAN_CATEGORY = "storage_plan"

_NOT_CACHED = object()


def product_category(product) -> str | None:
    """Return a Stripe product's pricing category."""
    return (getattr(product, "metadata", None) or {}).get("category")


@dataclass
class ProductMetadata:
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
    metered: bool = False
    price_ids: dict = field(default_factory=dict)

    def price_id_for_plan(self, plan: ProductMetadata) -> str | None:
        """This metered product's price as it applies to "plan".

        Keyed by the plan's Stripe product id, so a new plan needs only a price here
        rather than a change to the lookup.
        """
        if not self.metered:
            return None
        return self.price_ids.get(plan.stripe_id)


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
)

USAGE_BASED_STORAGE = ProductMetadata(
    stripe_id="prod_VNkBxaDz3cZbza",
    name="Usage-Based Storage",
    storage_limit_gb=0,  # Lifts the plan quota; the meter bills the difference.
    metered=True,
    price_ids={
        VERITY_FREE.stripe_id: "price_1UN6d3BR3saQICXCgJHfZd7O",  # free plans get a 500 MB free tier
        VERITY_PRO.stripe_id: "price_1UN6e5BR3saQICXCgQ5ViNNZ",  # paid plans get a 5000 MB free tier
    },
    features=[features.USAGE_BASED_BILLING, features.NO_STORAGE_LIMIT, features.PRICE],
)

PRODUCTS = {
    VERITY_PRO.stripe_id: VERITY_PRO,
    VERITY_FREE.stripe_id: VERITY_FREE,
    USAGE_BASED_STORAGE.stripe_id: USAGE_BASED_STORAGE,
}


def subscriptions_for_customers(customer_ids: list) -> list:
    """Return the subscriptions for Stripe customer ids, with their items prefetched."""
    if not customer_ids:
        return []
    return list(
        Subscription.objects.filter(customer_id__in=customer_ids).prefetch_related(
            Prefetch(
                "items",
                queryset=SubscriptionItem.objects.select_related("price", "price__product"),
            )
        )
    )


def linked_customer_ids(user, exclude_pk=None) -> list:
    """Return the Stripe ids of customers linked to "user", excluding their own FK one."""
    return list(
        Customer.objects.filter(subscriber=user).exclude(pk=exclude_pk).values_list("id", flat=True)
    )


def prime_active_subscriptions(users) -> None:
    """Prime the per-user subscription memo for many users in three queries.

    Batch callers otherwise re-walk each user's subscriptions individually, which
    made per-user work linear in queries.
    """
    users = list(users)
    if not users:
        return

    direct_customer_ids = [user.customer.id for user in users if getattr(user, "customer", None)]
    # One query for every customer these users own or are linked to. A subscriber
    # can own many customers (duplicates from repeated checkouts), so keep them all.
    owned: dict = {}
    for subscriber_id, customer_id in Customer.objects.filter(subscriber__in=users).values_list(
        "subscriber_id", "id"
    ):
        owned.setdefault(subscriber_id, []).append(customer_id)
    customer_ids = set(direct_customer_ids).union(*owned.values())
    subscriptions = subscriptions_for_customers(sorted(customer_ids))
    by_customer: dict = {}
    for subscription in subscriptions:
        by_customer.setdefault(subscription.customer_id, []).append(subscription)

    for user in users:
        own_id = user.customer.id if getattr(user, "customer", None) else None
        user_customer_ids = [own_id] if own_id else []
        user_customer_ids += [cid for cid in owned.get(user.pk, []) if cid != own_id]
        collected = [sub for cid in user_customer_ids for sub in by_customer.get(cid, [])]
        direct = getattr(user, "subscription", None)
        if direct is not None and not any(sub.pk == direct.pk for sub in collected):
            collected.append(direct)
        user._pt_active_subscriptions = [
            sub
            for sub in collected
            if getattr(sub, "status", None) in ACTIVE_SUBSCRIPTION_STATUSES
            or bool(getattr(sub, "cancel_at_period_end", False))
        ]


def _active_subscriptions(user) -> list:
    """Return the user's active subscriptions, from the customer plus the direct FK."""

    cached = getattr(user, "_pt_active_subscriptions", _NOT_CACHED)
    if cached is not _NOT_CACHED:
        return cast(list, cached)
    if not getattr(user, "is_authenticated", False):
        return []

    customer = getattr(user, "customer", None)

    # Every customer linked to this user in one query. Walking them individually cost
    # two queries each, on the path of every request that resolves a plan.
    customer_ids = [customer.id] if customer is not None else []
    customer_ids += linked_customer_ids(user, exclude_pk=customer.pk if customer else None)
    subscriptions = subscriptions_for_customers(customer_ids)
    direct = getattr(user, "subscription", None)
    if direct is not None and not any(sub.pk == direct.pk for sub in subscriptions):
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
    """Return the products the user is actively subscribed to, keyed by product id. A product can appear on two subscriptions at once (the old one whose
    cancellation has not landed yet, a webhook-synced duplicate), so the most recently created subscription wins rather than the product stacking."""
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

    "metered" is the discriminator rather than the category: Free and Pro share
    the "base_plan" category, and only usage-based storage is metered.
    """
    return next(
        (meta for meta in _active_products(user).values() if not meta.metered),
        VERITY_FREE,
    )


def active_products_for_user(user) -> list[ProductMetadata]:
    """Return every product the user holds."""
    return sorted(
        _active_products(user).values(),
        key=lambda meta: (meta.metered, meta.name),
    )


def has_metered_storage(user) -> bool:
    """True when the user has subscribed to usage-based storage."""
    product_id = USAGE_BASED_STORAGE.stripe_id
    return any(
        item.price is not None and item.price.product_id == product_id
        for subscription in _active_subscriptions(user)
        for item in subscription.items.all()
    )


def metered_price_id(product: ProductMetadata, user) -> str | None:
    """Return the Stripe price id for "product" as it applies to "user"."""
    if not product.metered:
        return None
    return product.price_id_for_plan(plan_for_user(user))
