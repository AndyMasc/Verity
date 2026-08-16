"""Stripe webhook handling for the reimbursements pipeline.

Flow: djstripe fires a signal for each event -> we enqueue a Dramatiq task ->
"process_stripe_event" runs the matching handler inside an atomic block and
records an idempotency marker so redelivered events are skipped. Handlers
mutate package/payment state via the model methods in models.py.
"""

import logging
from typing import Any

import djstripe.signals as djstripe_signals
import stripe
from django.db import transaction
from django.db.models import Q
from django.dispatch import receiver

from core.currencies import ZERO_DECIMAL_CURRENCIES
from records.models import AuditLog

from . import services
from .models import (
    PackagePayment,
    ProcessedStripeEvent,
    ReimbursementPackage,
    StripeAccount,
)

logger = logging.getLogger(__name__)

HANDLED_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "account.updated",
        "transfer.failed",
        "charge.failed",
        "charge.refunded",
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
    }
)


@receiver(djstripe_signals.webhook_post_process)
def enqueue_reimbursement_processing(**kwargs: Any) -> None:
    """Hands the Stripe event to the reimbursements pipeline as a Dramatiq task."""
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    if _event_type_from_trigger(trigger) not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    transaction.on_commit(lambda: process_stripe_event_task.send(trigger.id))


@receiver(djstripe_signals.webhook_processing_error)
def enqueue_reimbursement_processing_on_error(**kwargs: Any) -> None:
    """Hands the event to the reimbursements pipeline even when djstripe's sync fails."""
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    if _trigger_event_type(trigger) not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    process_stripe_event_task.send(trigger.id)


def _trigger_event_type(trigger: Any) -> str:
    """Best-effort event type for a trigger, from its raw payload or synced Event."""
    try:
        event_type = (trigger.json_body or {}).get("type", "")
        return event_type if event_type else _event_type_from_trigger(trigger)
    except Exception:
        logger.debug("Unable to read event type from webhook JSON body", exc_info=True)
        return _event_type_from_trigger(trigger)


def _event_type_from_trigger(trigger: Any) -> str:
    """Extract event type from trigger's synced Event."""
    return getattr(getattr(trigger, "event", None), "type", "")


def _as_dict(obj):
    """Normalizes webhook payload objects (dict or stripe.StripeObject)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return dict(obj)


def _has_package_metadata(session: dict) -> bool:
    """True when the session's metadata points at one of our packages."""
    return bool(dict(session.get("metadata") or {}).get("package_uuid"))


def _payment_for_checkout_session(session_id: str) -> PackagePayment | None:
    """Resolve the local payment row for a Checkout session id.

    Returns None when the session is unknown to Stripe (nothing to do). When
    the session exists on Stripe but the local payment row is missing, the
    original "DoesNotExist" is re-raised so the task is redelivered and the
    lookup is retried.
    """
    try:
        return PackagePayment.objects.select_related("package", "payer").get(
            stripe_checkout_session_id=session_id
        )
    except PackagePayment.DoesNotExist:
        try:
            services.retrieve_checkout_session(session_id)
        except stripe.error.InvalidRequestError:
            logger.error(
                "No PackagePayment for session %s and it is unknown to Stripe — skipping",
                session_id,
            )
            return None
        except stripe.error.StripeError:
            pass
        logger.error("No PackagePayment for session %s — raising for retry", session_id)
        raise


def apply_paid_session(payment, session, *, source: str) -> bool:
    """Record a settled Checkout Session against a payment and its package."""
    session_data = _as_dict(session)
    if not payment.amount_matches(session_data):
        return False

    already_completed = payment.is_completed
    payment.complete_from_session(session_data)
    package = payment.package
    payer_currency = payment.payer_currency or "usd"
    package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)

    if not already_completed:
        AuditLog.objects.create(
            user=package.creator,
            action=AuditLog.Action.UPDATE_RECORD,
            details={
                "event": source,
                "package_uuid": str(package.uuid),
                "stripe_session_id": session_data.get("id"),
                "payer_email": payment.payer.email if payment.payer else None,
                "amount": str(payment.amount_paid),
            },
        )
        transaction.on_commit(
            lambda: _notify_package_paid(package.pk, payment.payer.pk if payment.payer else None)
        )
    return True


def _retrieve_stripe_object(retriever, obj_id: str, obj_type: str):
    """Safely retrieve a Stripe object, logging errors appropriately."""
    try:
        return _as_dict(retriever(obj_id))
    except stripe.error.InvalidRequestError:
        logger.error("%s %s unknown to Stripe — skipping", obj_type, obj_id)
        return None
    except stripe.error.StripeError:
        raise


def _get_or_link_payment(package_uuid: str, payment_intent_id: str):
    """Get or link a payment to a PaymentIntent."""
    payment = (
        PackagePayment.objects.select_related("package", "payer")
        .filter(package__uuid=package_uuid)
        .filter(Q(stripe_payment_intent_id="") | Q(stripe_payment_intent_id__isnull=True))
        .order_by("-created_at")
        .first()
    )
    if payment is not None:
        payment.stripe_payment_intent_id = payment_intent_id
        payment.save(update_fields=["stripe_payment_intent_id"])
    return payment


def _payment_for_payment_intent(payment_intent_id: str):
    """Resolve a package payment from a Stripe PaymentIntent id."""
    payment = (
        PackagePayment.objects.select_related("package", "payer")
        .filter(stripe_payment_intent_id=payment_intent_id)
        .first()
    )
    if payment is not None:
        return payment

    payment_intent = _retrieve_stripe_object(
        services.retrieve_payment_intent, payment_intent_id, "PaymentIntent"
    )
    if payment_intent is None:
        return None

    package_uuid = dict(payment_intent.get("metadata") or {}).get("package_uuid")
    if not package_uuid:
        return None

    return _get_or_link_payment(package_uuid, payment_intent_id)


def _payment_from_charge(charge_id: str):
    """Resolve a package payment from a Stripe Charge id via its PaymentIntent."""
    charge = _retrieve_stripe_object(services.retrieve_charge, charge_id, "Charge")
    if charge is None:
        return None

    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return None
    return _payment_for_payment_intent(payment_intent_id)


def _revert_package_payment(payment, *, event: str, **extra) -> None:
    """Mark a payment failed and revert its package to open, with an audit log."""
    payment.mark_failed()
    payment.package.mark_as_refunded()
    AuditLog.objects.create(
        user=payment.package.creator,
        action=AuditLog.Action.UPDATE_RECORD,
        details={
            "event": event,
            "package_uuid": str(payment.package.uuid),
            **extra,
        },
    )


def _restore_paid_payment(payment, *, event: str, **extra) -> None:
    """Restore a payment/package that a won dispute brought back to the platform."""
    package = payment.package
    was_paid = payment.is_completed and package.status == ReimbursementPackage.Status.PAID
    payment.is_completed = True
    payment.save(update_fields=["is_completed"])
    payer_currency = getattr(payment, "payer_currency", None) or "usd"
    package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)
    if not was_paid:
        AuditLog.objects.create(
            user=package.creator,
            action=AuditLog.Action.UPDATE_RECORD,
            details={
                "event": event,
                "package_uuid": str(package.uuid),
                **extra,
            },
        )


def _handle_checkout_session_completed(event):
    """Apply a settled Checkout Session to a package payment."""
    session = _as_dict(event["data"]["object"])
    if session.get("payment_status") != "paid" or not _has_package_metadata(session):
        return

    payment = _payment_for_checkout_session(session["id"])
    if payment is None:
        return

    apply_paid_session(payment, session, source="package_paid")


def _handle_async_payment(event):
    """Handle async Checkout payment success/failure events."""
    session = _as_dict(event["data"]["object"])
    if not _has_package_metadata(session):
        return

    payment = _payment_for_checkout_session(session["id"])
    if payment is None:
        return

    package_uuid = session["metadata"]["package_uuid"]
    if event["type"] == "checkout.session.async_payment_succeeded":
        if not apply_paid_session(payment, session, source="async_payment_succeeded"):
            logger.error(
                "Package %s: session %s amount check failed — skipping mark-as-paid",
                package_uuid,
                session["id"],
            )
        return

    payment.mark_failed()
    logger.warning("Async payment failed for session %s (package %s)", session["id"], package_uuid)


def _handle_account_updated(event):
    """Sync Stripe account status fields to the local account row."""
    account = _as_dict(event["data"]["object"])
    account_id = account.get("id")
    if not account_id:
        return
    StripeAccount.objects.filter(stripe_account_id=account_id).update(
        stripe_details_submitted=account.get("details_submitted", False),
        charges_enabled=account.get("charges_enabled", False),
        payouts_enabled=account.get("payouts_enabled", False),
    )


def _handle_payment_failure(event):
    """Revert a payment when a Stripe transfer/charge failure occurs."""
    obj = _as_dict(event["data"]["object"])
    failure_message = obj.get("failure_message") or "unknown reason"

    payment_intent_id = obj.get("payment_intent")
    payment = None
    if payment_intent_id:
        payment = _payment_for_payment_intent(payment_intent_id)
    elif event["type"] == "transfer.failed" and obj.get("source_transaction"):
        payment = _payment_from_charge(obj["source_transaction"])
        if payment is not None and payment.stripe_payment_intent_id:
            payment_intent_id = payment.stripe_payment_intent_id

    logger.error(
        "Stripe %s — payment_intent: %s, reason: %s",
        event["type"],
        payment_intent_id,
        failure_message,
    )
    if payment is None:
        logger.warning(
            "No PackagePayment found for %s (payment_intent: %s)",
            event["type"],
            payment_intent_id,
        )
        return

    _revert_package_payment(
        payment,
        event=event["type"],
        payment_intent=payment_intent_id,
        failure_message=failure_message,
    )


def _handle_charge_refunded(event):
    """Handle charge refunds, distinguishing full reversals from partial refunds."""
    charge = _as_dict(event["data"]["object"])
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return

    charge_currency = charge.get("currency", "usd")
    amount_refunded_cents = charge.get("amount_refunded") or 0
    amount_captured_cents = charge.get("amount_captured") or 0

    refunded_display = (
        float(amount_refunded_cents)
        if charge_currency.lower() in ZERO_DECIMAL_CURRENCIES
        else amount_refunded_cents / 100
    )

    logger.warning(
        "Charge refunded — payment_intent: %s, amount: %s %.2f",
        payment_intent_id,
        charge_currency.upper(),
        refunded_display,
    )

    payment = _payment_for_payment_intent(payment_intent_id)
    if payment is None:
        logger.warning("No PackagePayment found for refunded payment_intent %s", payment_intent_id)
        return

    is_full_refund = amount_refunded_cents >= amount_captured_cents
    if is_full_refund:
        _revert_package_payment(
            payment,
            event="charge_refunded",
            payment_intent=payment_intent_id,
            amount_refunded_cents=amount_refunded_cents,
            amount_captured_cents=amount_captured_cents,
            is_full_refund=True,
        )
        return

    # Partial refund: log details for tracking without marking the whole payment failed
    AuditLog.objects.create(
        user=payment.package.creator,
        action=AuditLog.Action.UPDATE_RECORD,
        details={
            "event": "charge_refunded",
            "package_uuid": str(payment.package.uuid),
            "payment_intent": payment_intent_id,
            "amount_refunded_cents": amount_refunded_cents,
            "amount_captured_cents": amount_captured_cents,
            "is_full_refund": False,
        },
    )


def _handle_dispute(event):
    """Handle Stripe dispute lifecycle events."""
    dispute = _as_dict(event["data"]["object"])
    dispute_id = dispute.get("id") or "unknown"
    status = dispute.get("status") or "unknown"
    charge_id = dispute.get("charge")
    logger.warning(
        "Stripe dispute %s (status: %s) — charge: %s, amount: %s %s",
        dispute_id,
        status,
        charge_id,
        (dispute.get("currency") or "").upper(),
        dispute.get("amount") or 0,
    )
    if not charge_id:
        return

    payment = _payment_from_charge(charge_id)
    if payment is None:
        logger.warning("No PackagePayment found for disputed charge %s", charge_id)
        return

    if status == "won":
        _restore_paid_payment(
            payment,
            event="charge_dispute_won",
            dispute_id=dispute_id,
        )
        return

    _revert_package_payment(
        payment,
        event="charge_dispute",
        dispute_id=dispute_id,
        status=status,
    )


_EVENT_HANDLERS = {
    "checkout.session.completed": _handle_checkout_session_completed,
    "checkout.session.async_payment_succeeded": _handle_async_payment,
    "checkout.session.async_payment_failed": _handle_async_payment,
    "account.updated": _handle_account_updated,
    "transfer.failed": _handle_payment_failure,
    "charge.failed": _handle_payment_failure,
    "charge.refunded": _handle_charge_refunded,
    "charge.dispute.created": _handle_dispute,
    "charge.dispute.updated": _handle_dispute,
    "charge.dispute.closed": _handle_dispute,
}


@transaction.atomic
def process_stripe_event(event):
    """Applies a single Stripe webhook event to the reimbursements flow safely.

    Idempotency: redelivered events (Stripe redelivers; Dramatiq retries on
    failure) are skipped via the "ProcessedStripeEvent" marker, recorded
    inside the same atomic block as the handler so a failed run retries whole.
    """
    event_data = _as_dict(event)
    event_id = event_data.get("id")

    if event_id and ProcessedStripeEvent.objects.filter(event_id=event_id).exists():
        logger.info("Stripe event %s already processed — skipping", event_id)
        return

    handler = _EVENT_HANDLERS.get(event_data.get("type"))
    if handler is not None:
        handler(event_data)

    if event_id:
        ProcessedStripeEvent.objects.create(event_id=event_id)


def _notify_package_paid(package_pk: int, payer_pk: int | None) -> None:
    from .tasks import send_package_paid_notification_task

    send_package_paid_notification_task.send(package_pk, payer_pk)
