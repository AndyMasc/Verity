from __future__ import annotations

from dataclasses import dataclass

from djstripe.models import Customer, ProductFeature, Subscription

from teams.models import PersonalOrg, SponsoredOrg

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
        features.MULTI_CURRENCY,
    ],
    monthly_upload_limit=features.FREE_MONTHLY_UPLOAD_LIMIT,
    monthly_scan_limit=features.FREE_MONTHLY_SCAN_LIMIT,
)

PLUS = ProductMetadata(
    stripe_id="prod_VC8WUN1RO4Apqx",
    monthly_upload_limit=features.PRO_MONTHLY_UPLOAD_LIMIT,
    monthly_scan_limit=features.PRO_MONTHLY_SCAN_LIMIT,
)

ORGANIZATIONS = ProductMetadata(
    stripe_id="prod_VRCG4ddJeaMqzl",
    features=["create-orgs", "add-org-members"],
    monthly_upload_limit=features.PRO_MONTHLY_UPLOAD_LIMIT,
    monthly_scan_limit=features.PRO_MONTHLY_SCAN_LIMIT,
)

USAGE_BASED_STORAGE = ProductMetadata(
    stripe_id="prod_VNkBxaDz3cZbza",
    metered=True,
)

PRODUCTS = {
    PLUS.stripe_id: PLUS,
    ORGANIZATIONS.stripe_id: ORGANIZATIONS,
    VERITY_FREE.stripe_id: VERITY_FREE,
    USAGE_BASED_STORAGE.stripe_id: USAGE_BASED_STORAGE,
}


def org_customer(org) -> Customer | None:
    """The Stripe customer backing an org's subscription."""
    return getattr(getattr(org, "billing", None), "customer", None)


def active_subscriptions(org) -> list:
    """The org's active subscriptions."""
    if org is None:
        return []

    cached = getattr(org, "_active_subs", None)
    if cached is not None:
        return cached

    customer = org_customer(org)
    subscriptions = (
        Subscription.objects.filter(customer=customer)
        .prefetch_related("items__price__product")
        .order_by("pk")
        if customer is not None
        else []
    )
    org._active_subs = [s for s in subscriptions if is_active_subscription(s)]
    return org._active_subs


def live_items(subscription) -> list:
    """Held items, minus the rows dj-stripe leaves behind for items Stripe has removed."""
    payload = (subscription.stripe_data or {}).get("items")
    if payload is None:
        return [item for item in subscription.items.all() if item.price]
    live = {entry["price"]["id"] for entry in payload["data"] if entry.get("price", {}).get("id")}
    return [item for item in subscription.items.all() if item.price and item.price.id in live]


def payer_still_holds(sponsored) -> bool:
    """A sponsorship lasts exactly as long as its sponsor holds that plan.

    Reads the sponsor's personal org through their marker rather than importing
    teams.orgs, which would close a cycle back through billing.models.
    """
    try:
        sponsor_org = sponsored.sponsor.personal_org.organization
    except PersonalOrg.DoesNotExist:
        return False
    return sponsored.product_id in {
        item.price.product.id
        for sub in active_subscriptions(sponsor_org)
        for item in live_items(sub)
    }


def held_products(org) -> dict[str, ProductMetadata]:
    """Products the org holds, keyed by Stripe product id.

    A sponsored org runs on the plan its sponsor bought, on top of anything the
    org holds itself.
    """
    held = {
        meta.stripe_id: meta
        for subscription in active_subscriptions(org)
        for item in live_items(subscription)
        if (meta := PRODUCTS.get(item.price.product.id))
    }

    if org is None:
        return held

    sponsored = SponsoredOrg.objects.filter(organization=org).first()
    if sponsored and payer_still_holds(sponsored):
        granted = PRODUCTS.get(sponsored.product_id)
        if granted:
            held[granted.stripe_id] = granted

    return held


def plan_for_org(org) -> ProductMetadata:
    """The org's plan, or Free. Metered add-ons are excluded."""
    return next((m for m in held_products(org).values() if not m.metered), VERITY_FREE)


def has_metered_storage(org) -> bool:
    return USAGE_BASED_STORAGE.stripe_id in held_products(org)


def is_active_subscription(subscription) -> bool:
    """True while the subscription grants access, including up to a scheduled cancel."""
    return getattr(subscription, "status", None) in ACTIVE_SUBSCRIPTION_STATUSES or bool(
        getattr(subscription, "cancel_at_period_end", False)
    )


def granted_features(org) -> set[str]:
    """lookup_keys of the Stripe features attached to the products "org" holds."""
    return {
        product_feature.entitlement_feature.stripe_data.get("lookup_key")
        for product_feature in ProductFeature.objects.filter(
            product_id__in=held_products(org)
        ).select_related("entitlement_feature")
    }
