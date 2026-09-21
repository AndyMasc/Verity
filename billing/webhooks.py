import logging
from typing import Any

import djstripe.signals as djstripe_signals
from django.db import transaction
from django.dispatch import receiver
from djstripe.event_handlers import djstripe_receiver

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

    from .context_processors import invalidate_subscription_status_cache
    from .models import CustomUser

    user_ids = list(
        CustomUser.objects.filter(customer__id=customer_id).values_list("id", flat=True)
    )
    for user_id in user_ids:
        invalidate_subscription_status_cache(user_id)


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
