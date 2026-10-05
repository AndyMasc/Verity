import logging
from typing import Any

import djstripe.signals as djstripe_signals
from django.core.cache import cache
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone
from djstripe.event_handlers import djstripe_receiver
from djstripe.models import Customer, Subscription

from core.apps import posthog_client

from .services import SETTLED_PAYMENT_STATUSES

logger = logging.getLogger(__name__)


@receiver(djstripe_signals.webhook_processing_error)
def report_webhook_processing_error(**kwargs: Any) -> None:
    """Logs djstripe webhook processing failures."""
    trigger = kwargs.get("instance")
    exception = kwargs.get("exception")
    logger.error(
        "djstripe webhook processing failed (trigger=%s): %s",
        trigger,
        exception,
        exc_info=exception,
    )


def _event_object(event: Any) -> dict[str, Any]:
    """Return the Stripe object carried by a webhook event as a plain dict."""
    return (getattr(event, "data", None) or {}).get("object") or {}


def _subscriber_pk(customer_id: str | None, client_reference_id: str | None = None) -> str | None:
    """Resolve the local user behind a Stripe event, or None when unlinked. The client reference ID is the id we stamp on our own checkout sessions, so
    it is the most reliable link; the customer's subscriber covers events that carry no session (invoices, payment intents)."""
    if client_reference_id and str(client_reference_id).isdigit():
        from .models import CustomUser

        if CustomUser.objects.filter(pk=int(client_reference_id)).exists():
            return str(int(client_reference_id))

    if not customer_id:
        return None

    subscriber_id = (
        Customer.objects.filter(id=customer_id).values_list("subscriber_id", flat=True).first()
    )
    return str(subscriber_id) if subscriber_id else None


def capture(event: str, distinct_id: str | None, properties: dict[str, Any]) -> None:
    """Capture a PostHog event from a Stripe webhook."""
    if posthog_client is None:
        return

    if distinct_id:
        posthog_client.capture(event, distinct_id=distinct_id, properties=properties)
    else:
        posthog_client.capture(event, properties=properties)


def failure_reason(failure: dict[str, Any]) -> str:
    """Return a human-readable reason for a failed payment."""
    return failure.get("message") or failure.get("code") or "unknown"


# Cancellations fall into three kinds: a user cancelling at period end, a user cancelling immediately, and this app tidying up after itself.
# The intent is recorded when we first see it and cacged here when we cause it.
_CANCEL_MARKER = "billing:cancel-intent:{sub_id}"
_CANCEL_MARKER_TTL = 60 * 60 * 24


def mark_cancel_intent(sub_id: str, kind: str) -> None:
    """Record how a subscription's cancellation came about, for the webhook."""
    cache.set(_CANCEL_MARKER.format(sub_id=sub_id), kind, _CANCEL_MARKER_TTL)


def read_cancel_intent(sub_id: str) -> str | None:
    return cache.get(_CANCEL_MARKER.format(sub_id=sub_id))


def clear_cancel_intent(sub_id: str) -> None:
    cache.delete(_CANCEL_MARKER.format(sub_id=sub_id))


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

    customer_id = stripe_sub.get("customer")
    if stripe_sub.get("cancel_at_period_end"):
        sub_id = stripe_sub.get("id")
        if sub_id and read_cancel_intent(sub_id) is None:
            mark_cancel_intent(sub_id, "period_end")
            capture_subscription_cancelled(sub_id, stripe_sub)

    capture("subscription_updated", _subscriber_pk(customer_id), properties={})


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
    if read_cancel_intent(sub_id) != "period_end":
        capture_subscription_cancelled(sub_id, stripe_sub)

    def _clear_user_subscription() -> None:
        from .models import CustomUser

        updated_count = CustomUser.objects.filter(subscription__id=sub_id).update(subscription=None)
        if updated_count:
            logger.info("Cleared subscription %s from %d user(s).", sub_id, updated_count)

    transaction.on_commit(_clear_user_subscription)
    transaction.on_commit(lambda: clear_cancel_intent(sub_id))


def capture_subscription_cancelled(sub_id: str, stripe_sub: dict | None = None) -> None:
    """Record churn for a cancelled subscription, split by what it covered. Cancellations this app performed itself are skipped: they are consequences
    of another change (a plan swap, or an add-on following its base plan out) rather than a decision to leave."""
    if posthog_client is None:
        return

    sub = Subscription.objects.filter(id=sub_id).select_related("customer__subscriber").first()
    if sub is None:
        return

    user = getattr(getattr(sub, "customer", None), "subscriber", None)
    # Skip our own cancellations: a plan swap or an add-on following its base plan out is not a user deciding to leave.
    if read_cancel_intent(sub_id) == "system":
        return

    cancel_type = read_cancel_intent(sub_id) or (
        "period_end" if (stripe_sub or {}).get("cancel_at_period_end") else "immediate"
    )

    months_active = None
    if sub.created:
        months_active = round((timezone.now() - sub.created).days / 30.44, 1)

    distinct_id = str(user.pk) if user is not None else None
    common = {
        "months_active": months_active,  # How long the subscription was active before cancellation.
        "cancel_type": cancel_type,  # Whether the user cancelled immediately, at period end, or the app cancelled it automatically.
    }
    capture("subscription_cancelled", distinct_id, properties=common)


@djstripe_receiver("checkout.session.completed")
@djstripe_receiver("checkout.session.async_payment_succeeded")
def handle_checkout_settled(**kwargs: Any) -> None:
    """Track subscription checkouts Stripe actually settled. Anyone who finishes checkout without reaching the success URL is still counted. Delayed payment methods
    settle later via checkout.session.async_payment_succeeded, so their checkout.session.completed (sent unpaid) is skipped here."""
    session = _event_object(kwargs.get("event"))
    if session.get("mode") != "subscription":
        return
    # "no_payment_required" covers fully discounted checkouts.
    if session.get("payment_status") not in SETTLED_PAYMENT_STATUSES:
        return

    capture(
        "subscription_checkout_completed",
        _subscriber_pk(session.get("customer"), session.get("client_reference_id")),
        {
            "amount": (session.get("amount_total") or 0) / 100,
            "currency": session.get("currency"),
        },
    )


@djstripe_receiver("checkout.session.expired")
def handle_checkout_expired(**kwargs: Any) -> None:
    """Track abandoned subscription checkouts to complete the funnel."""
    session = _event_object(kwargs.get("event"))
    if session.get("mode") != "subscription":
        return

    capture(
        "subscription_checkout_expired",
        _subscriber_pk(session.get("customer"), session.get("client_reference_id")),
        {
            "amount": (session.get("amount_total") or 0) / 100,
            "currency": session.get("currency"),
        },
    )


@djstripe_receiver("invoice.payment_succeeded")
def handle_payment_succeeded(**kwargs: Any) -> None:
    """Track settled subscription invoices, both first payments and renewals."""
    invoice = _event_object(kwargs.get("event"))

    capture(
        "payment_succeeded",
        _subscriber_pk(invoice.get("customer")),
        {
            "amount": (invoice.get("amount_paid") or 0) / 100,
            "currency": invoice.get("currency"),
            "billing_reason": invoice.get("billing_reason"),
        },
    )


@djstripe_receiver("payment_intent.payment_failed")
def handle_checkout_payment_failed(**kwargs: Any) -> None:
    """Track declined checkout payments, the main cause of checkout abandonment."""
    payment_intent = _event_object(kwargs.get("event"))
    if payment_intent.get("invoice"):
        # Renewal decline: invoice.payment_failed already reports it.
        return
    if (payment_intent.get("metadata") or {}).get("package_uuid"):
        # Package purchase decline: owned by the reimbursements pipeline.
        return

    capture(
        "checkout_payment_failed",
        _subscriber_pk(payment_intent.get("customer")),
        {
            "amount": (payment_intent.get("amount") or 0) / 100,
            "currency": payment_intent.get("currency"),
            "failure_reason": failure_reason(payment_intent.get("last_payment_error") or {}),
        },
    )


@djstripe_receiver("invoice.payment_failed")
def handle_payment_failed(**kwargs: Any) -> None:
    """Track failed subscription payments."""
    invoice = _event_object(kwargs.get("event"))
    distinct_id = _subscriber_pk(invoice.get("customer"))
    if not distinct_id:
        return

    amount_due = invoice.get("amount_due") or invoice.get("amount") or 0

    capture(
        "payment_failed",
        distinct_id,
        {
            "amount": amount_due / 100,
            "currency": invoice.get("currency", "usd"),
            "failure_reason": failure_reason(invoice.get("last_payment_error") or {}),
        },
    )
