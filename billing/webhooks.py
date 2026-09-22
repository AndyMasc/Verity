import logging
from typing import Any

import djstripe.signals as djstripe_signals
import stripe
from django.db import transaction
from django.dispatch import receiver
from djstripe.event_handlers import djstripe_receiver
from djstripe.models import Subscription

from . import metadata, services

logger = logging.getLogger(__name__)


@receiver(djstripe_signals.webhook_processing_error)
def report_webhook_processing_error(**kwargs: Any) -> None:
    """Logs (and surfaces to Sentry) djstripe webhook processing failures."""
    trigger = kwargs.get(
        "instance"
    )  # which specific endpoint or webhook transmission attempt failed.
    exception = kwargs.get("exception")  # python traceback and error message
    logger.error(
        "djstripe webhook processing failed (trigger=%s): %s",
        trigger,
        exception,
        exc_info=exception,
    )


def _subscription_categories(stripe_sub: dict) -> set[str]:
    """Return the pricing categories covered by a Stripe subscription payload."""
    categories: set[str] = set()
    for item in (stripe_sub or {}).get("items", {}).get("data", []):
        product_id = item.get("price", {}).get("product")
        category = metadata.category_for_product(product_id)
        if category:
            categories.add(category)
    return categories


def _base_plan_is_ending(stripe_sub: dict) -> bool:
    """True when a base plan is being canceled or scheduled for cancellation."""
    if not stripe_sub:
        return False
    status = stripe_sub.get("status")
    if status in {"canceled", "unpaid", "incomplete_expired"}:
        return True
    return bool(stripe_sub.get("cancel_at_period_end"))


def _cancel_pro_only_storage_for_customer(
    customer_id: str, base_subscription_id: str | None = None
) -> None:
    """Cancel any active Pro-only storage add-ons after the user's Pro base plan ends."""
    if not customer_id:
        return

    queryset = Subscription.objects.filter(customer__id=customer_id)
    if base_subscription_id:
        queryset = queryset.exclude(id=base_subscription_id)

    for sub in queryset.iterator():
        status = (sub.stripe_data or {}).get("status")
        if status not in {"active", "trialing"}:
            continue

        for item in sub.items.select_related("price__product").all():
            product = item.price.product if item.price else None
            if product is None:
                continue
            meta = metadata.PRODUCTS.get(product.id)
            if meta is not None and meta.category == "storage_plan" and meta.pro_only:
                try:
                    services.cancel_subscription(sub.id)
                    logger.warning(
                        "Auto-canceled pro-only storage pack %s for customer %s because base plan %s ended.",
                        sub.id,
                        customer_id,
                        base_subscription_id,
                    )
                except stripe.error.StripeError as exc:
                    logger.warning(
                        "Failed to auto-cancel pro-only storage pack %s for customer %s after base plan cancellation: %s",
                        sub.id,
                        customer_id,
                        exc,
                    )
                break


@djstripe_receiver("customer.subscription.deleted")
def handle_subscription_deleted(**kwargs: Any) -> None:
    """Clears the user's subscription relation when cancelled in Stripe."""
    event = kwargs.get("event")
    if not event:
        return

    stripe_sub = event.data.get("object", {})
    sub_id = stripe_sub.get("id")

    if not sub_id:
        return

    _invalidate_subscription_caches_for_event(stripe_sub)
    if "base_plan" in _subscription_categories(stripe_sub):
        _cancel_pro_only_storage_for_customer(
            stripe_sub.get("customer"), base_subscription_id=sub_id
        )

    def _clear_user_subscription() -> None:
        from .models import CustomUser

        updated_count = CustomUser.objects.filter(subscription__id=sub_id).update(subscription=None)
        if updated_count:
            logger.info("Cleared subscription %s from %d user(s).", sub_id, updated_count)

    transaction.on_commit(_clear_user_subscription)


def _invalidate_subscription_caches_for_event(stripe_sub: dict) -> None:
    """Drop the cached plan/subscription status for every user tied to a sub.

    Subscription lifecycle events (created/updated/deleted) change what the
    sidebar and billing context display, so the per-user "subscription_status"
    cache must be cleared for everyone who shares that Stripe customer.
    """
    customer_id = stripe_sub.get("customer")
    if not customer_id:
        return

    from .context_processors import invalidate_plan_usage_caches
    from .models import CustomUser

    user_ids = list(
        CustomUser.objects.filter(customer__id=customer_id).values_list("id", flat=True)
    )
    for user_id in user_ids:
        invalidate_plan_usage_caches(user_id)


@djstripe_receiver("customer.subscription.created")
@djstripe_receiver("customer.subscription.updated")
def handle_subscription_changed(**kwargs: Any) -> None:
    """Invalidate plan/subscription caches when a subscription is created or updated."""
    event = kwargs.get("event")
    if not event:
        return

    stripe_sub = event.data.get("object", {})
    if not stripe_sub.get("id"):
        return

    _invalidate_subscription_caches_for_event(stripe_sub)

    if "base_plan" in _subscription_categories(stripe_sub) and _base_plan_is_ending(stripe_sub):
        _cancel_pro_only_storage_for_customer(
            stripe_sub.get("customer"), base_subscription_id=stripe_sub.get("id")
        )
