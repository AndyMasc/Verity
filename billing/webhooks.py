import logging
from typing import Any

import djstripe.signals as djstripe_signals
import stripe
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone
from djstripe.event_handlers import djstripe_receiver
from djstripe.models import Customer, Subscription

from core.apps import posthog_client

from . import metadata, services

logger = logging.getLogger(__name__)


@receiver(djstripe_signals.webhook_processing_error)
def report_webhook_processing_error(**kwargs: Any) -> None:
    """Logs djstripe webhook processing failures."""
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


def _has_active_pro_storage(sub: Subscription) -> bool:
    """Return whether an active subscription contains a Pro-only storage pack."""
    if (sub.stripe_data or {}).get("status") not in {"active", "trialing"}:
        return False

    for item in sub.items.select_related("price__product").all():
        product = item.price.product if item.price else None
        product_meta = metadata.PRODUCTS.get(product.id) if product else None
        is_storage_plan = product_meta and product_meta.category == "storage_plan"
        if is_storage_plan and product_meta.pro_only:
            return True
    return False


def _cancel_storage_subscription(
    sub: Subscription, customer_id: str, base_subscription_id: str | None
) -> None:
    """Cancel a Pro-only storage subscription and log the result."""
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


def _cancel_pro_only_storage_for_customer(
    customer_id: str, base_subscription_id: str | None = None
) -> None:
    """Cancel active Pro-only storage add-ons after the user's base plan ends."""
    if not customer_id:
        return

    subscriptions = Subscription.objects.filter(customer__id=customer_id)
    if base_subscription_id:
        subscriptions = subscriptions.exclude(id=base_subscription_id)

    for sub in subscriptions.iterator():
        if _has_active_pro_storage(sub):
            _cancel_storage_subscription(sub, customer_id, base_subscription_id)


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
        _capture_subscription_cancelled(sub_id, stripe_sub)
        _cancel_pro_only_storage_for_customer(
            stripe_sub.get("customer"), base_subscription_id=sub_id
        )

    def _clear_user_subscription() -> None:
        from .models import CustomUser

        updated_count = CustomUser.objects.filter(subscription__id=sub_id).update(
            subscription=None
        )
        if updated_count:
            logger.info(
                "Cleared subscription %s from %d user(s).", sub_id, updated_count
            )

    transaction.on_commit(_clear_user_subscription)


def _invalidate_subscription_caches_for_event(stripe_sub: dict) -> None:
    """Drop the cached plan/subscription status for every user tied to a sub."""
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

    if "base_plan" in _subscription_categories(stripe_sub):
        _cancel_pro_only_storage_for_customer(
            stripe_sub.get("customer"), base_subscription_id=stripe_sub.get("id")
        )


def _capture_subscription_cancelled(
    sub_id: str, stripe_sub: dict | None = None
) -> None:
    """Track a base-plan cancellation for churn analysis."""
    if posthog_client is None:
        return

    sub = (
        Subscription.objects.filter(id=sub_id)
        .select_related("customer__subscriber")
        .first()
    )
    if sub is None:
        return

    user = getattr(getattr(sub, "customer", None), "subscriber", None)
    properties = {
        "plan": None,
        "months_active": None,
        "cancel_type": (
            "period_end"
            if (stripe_sub or {}).get("cancel_at_period_end")
            else "immediate"
        ),
    }

    item = sub.items.select_related("price__product").first()
    if item and item.price and item.price.product:
        properties["plan"] = item.price.product.name

    if sub.created:
        properties["months_active"] = round(
            (timezone.now() - sub.created).days / 30.44, 1
        )

    capture_opts: dict = {"properties": properties}
    if user is not None:
        capture_opts["distinct_id"] = str(user.pk)
    posthog_client.capture("subscription_cancelled", **capture_opts)


@djstripe_receiver("invoice.payment_failed")
def handle_payment_failed(**kwargs: Any) -> None:
    """Track failed subscription payments for churn and recovery flows."""
    event = kwargs.get("event")
    if not event:
        return

    invoice = event.data.get("object", {})
    customer_id = invoice.get("customer")
    if not customer_id or posthog_client is None:
        return

    customer = (
        Customer.objects.filter(id=customer_id, subscriber__isnull=False)
        .select_related("subscriber")
        .first()
    )
    if customer is None or customer.subscriber is None:
        return

    amount_due = invoice.get("amount_due") or invoice.get("amount") or 0
    failure = invoice.get("last_payment_error") or {}
    error_message = failure.get("message") or failure.get("code") or "unknown"

    posthog_client.capture(
        "payment_failed",
        distinct_id=str(customer.subscriber.pk),
        properties={
            "amount": amount_due / 100,
            "currency": invoice.get("currency", "usd"),
            "failure_reason": error_message,
        },
    )
