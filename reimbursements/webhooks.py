"""Stripe webhook handling: dj-stripe signal -> Dramatiq task -> atomic handler dispatch.

Events are deduplicated on their Stripe id so redeliveries are skipped.
"""

import logging
from typing import Any

import djstripe.signals as djstripe_signals
import stripe
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.dispatch import receiver
from django.utils import timezone
from djstripe.models import Charge, PaymentIntent, Session

from core.apps import posthog_client
from core.currencies import from_stripe_amount
from records.models import AuditLog

from . import services
from .models import (
    PackagePayment,
    ProcessedStripeEvent,
    ReimbursementPackage,
    StripeAccount,
)

logger = logging.getLogger(__name__)


def trigger_event_type(trigger: Any) -> str:
    """Synced event type, or the raw payload when dj-stripe has no Event row yet."""
    synced = getattr(getattr(trigger, "event", None), "type", "")
    if synced:
        return synced
    try:
        return (trigger.json_body or {}).get("type", "")
    except Exception:
        logger.debug("Unable to read event type from webhook JSON body", exc_info=True)
        return ""


@receiver(djstripe_signals.webhook_post_process)
def enqueue_reimbursement_processing(**kwargs: Any) -> None:
    trigger = kwargs.get("instance")
    if trigger is None or trigger_event_type(trigger) not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    transaction.on_commit(lambda: process_stripe_event_task.send(trigger.id))


@receiver(djstripe_signals.webhook_processing_error)
def enqueue_reimbursement_processing_on_error(**kwargs: Any) -> None:
    trigger = kwargs.get("instance")
    if trigger is None or trigger_event_type(trigger) not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    process_stripe_event_task.send(trigger.id)


def as_dict(obj):
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return dict(obj)


def has_package_metadata(session: dict) -> bool:
    return bool(dict(session.get("metadata") or {}).get("package_uuid"))


def payment_for_checkout_session(session_id: str) -> PackagePayment | None:
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
            # Transient Stripe failure. Fall through so the original
            # DoesNotExist is re-raised and the task is retried later.
            pass
        raise


def apply_paid_session(payment, session, *, source: str) -> bool:
    session_data = as_dict(session)
    if not payment.amount_matches(session_data):
        return False

    package = payment.package

    # A deleted package must never move money: refund the payer instead of
    # completing the settlement (the creator deleted it mid-checkout).
    if package.is_deleted:
        logger.warning(
            "Package %s was deleted before settlement of session %s — auto-refunding",
            package.uuid,
            session_data.get("id"),
        )
        refund_captured_payment(payment, event="package_deleted_before_settlement")
        payment.mark_failed()
        audit(package, "checkout_after_delete_refunded", stripe_session_id=session_data.get("id"))
        return False

    if package.status == ReimbursementPackage.Status.PAID and not payment.is_completed:
        logger.error(
            "Package %s is already paid — refusing duplicate settlement of session %s",
            package.uuid,
            session_data.get("id"),
        )
        refund_captured_payment(payment, event="duplicate_settlement")
        payment.mark_failed()
        return False

    already_completed = payment.is_completed
    payment.complete_from_session(session_data)
    payer_currency = payment.payer_currency or "usd"
    package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)

    if not already_completed:
        audit(
            package,
            source,
            stripe_session_id=session_data.get("id"),
            payer_email=payment.payer.email if payment.payer else None,
            amount=str(payment.amount_paid),
        )
        transaction.on_commit(
            lambda: notify_package_paid(package.pk, payment.payer.pk if payment.payer else None)
        )
    return True


def retrieve_stripe_object(retriever, obj_id: str, obj_type: str):
    try:
        return as_dict(retriever(obj_id))
    except stripe.error.InvalidRequestError:
        logger.error("%s %s unknown to Stripe — skipping", obj_type, obj_id)
        return None
    except stripe.error.StripeError:
        raise


def audit(package, event: str, **extra) -> None:
    AuditLog.objects.create(
        user=package.creator,
        action=AuditLog.Action.UPDATE_RECORD,
        details={"event": event, "package_uuid": str(package.uuid), **extra},
    )


def session_for_intent(payment_intent_id: str) -> str | None:
    """Checkout session that produced this PaymentIntent, from dj-stripe's local sync."""
    row = Session.objects.filter(payment_intent=payment_intent_id).only("id").first()
    return row.id if row else None


def intent_metadata(payment_intent_id: str) -> dict | None:
    row = PaymentIntent.objects.filter(id=payment_intent_id).only("metadata").first()
    if row is not None:
        return dict(row.metadata or {})
    fetched = retrieve_stripe_object(
        services.retrieve_payment_intent, payment_intent_id, "PaymentIntent"
    )
    return dict(fetched.get("metadata") or {}) if fetched else None


def link_payment(package_uuid: str, payment_intent_id: str):
    """Fallback link when no Session points at the intent; refuses to guess between candidates."""
    candidates = list(
        PackagePayment.objects.select_related("package", "payer")
        .filter(package__uuid=package_uuid)
        .filter(Q(stripe_payment_intent_id="") | Q(stripe_payment_intent_id__isnull=True))
        .order_by("-created_at")[:2]
    )
    if len(candidates) != 1:
        logger.error(
            "Cannot link payment intent %s to package %s: %d unlinked payments",
            payment_intent_id,
            package_uuid,
            len(candidates),
        )
        return None

    payment = candidates[0]
    payment.stripe_payment_intent_id = payment_intent_id
    payment.save(update_fields=["stripe_payment_intent_id"])
    return payment


def payment_for_payment_intent(payment_intent_id: str):
    payment = (
        PackagePayment.objects.select_related("package", "payer")
        .filter(stripe_payment_intent_id=payment_intent_id)
        .first()
    )
    if payment is not None:
        return payment

    session_id = session_for_intent(payment_intent_id)
    if session_id is not None:
        return PackagePayment.objects.filter(stripe_checkout_session_id=session_id).first()

    metadata = intent_metadata(payment_intent_id)
    if not metadata:
        return None
    return link_payment(metadata.get("package_uuid"), payment_intent_id)


def payment_from_charge(charge_id: str):
    row = Charge.objects.filter(id=charge_id).only("payment_intent").first()
    if row is not None:
        intent_id = row.payment_intent_id
    else:
        charge = retrieve_stripe_object(services.retrieve_charge, charge_id, "Charge")
        intent_id = charge.get("payment_intent") if charge else None

    return payment_for_payment_intent(intent_id) if intent_id else None


def revert_package_payment(payment, *, event: str, refund: bool = True, **extra) -> None:
    refunded = False
    if refund:
        refunded = refund_captured_payment(payment, event=event)
        if not refunded and payment.is_completed:
            audit(
                payment.package,
                "auto_refund_failed",
                payment_intent=payment.stripe_payment_intent_id,
                trigger=event,
                **extra,
            )
            logger.critical(
                "Package %s: captured charge %s could not be auto-refunded after %s — "
                "package left settled; manual refund required",
                payment.package.uuid,
                payment.stripe_payment_intent_id,
                event,
            )
            return

    payment.mark_failed()
    payment.package.mark_as_refunded()
    audit(payment.package, event, payer_refunded=refunded, **extra)


def refund_captured_payment(payment, *, event: str) -> bool:
    if not payment.is_completed:
        return False
    payment_intent_id = payment.stripe_payment_intent_id
    if not payment_intent_id:
        return False
    try:
        services.create_refund(payment_intent_id, reason=event)
    except stripe.error.InvalidRequestError as e:
        # Typically "already refunded" / "charge not capturable" — nothing more to do.
        logger.warning("Refund skipped for payment intent %s (%s): %s", payment_intent_id, event, e)
        return False
    except stripe.error.StripeError:
        logger.exception(
            "Auto-refund failed for payment intent %s after %s",
            payment_intent_id,
            event,
        )
        return False
    logger.warning(
        "Payer auto-refunded for package %s after %s (payment_intent=%s)",
        payment.package.uuid,
        event,
        payment_intent_id,
    )
    return True


def restore_paid_payment(payment, *, event: str, **extra) -> None:
    package = payment.package
    was_paid = payment.is_completed and package.status == ReimbursementPackage.Status.PAID
    payment.is_completed = True
    payment.save(update_fields=["is_completed"])
    payer_currency = getattr(payment, "payer_currency", None) or "usd"
    package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)
    if not was_paid:
        audit(package, event, **extra)


def handle_checkout_session_completed(event):
    session = as_dict(event["data"]["object"])
    if session.get("payment_status") != "paid" or not has_package_metadata(session):
        return

    payment = payment_for_checkout_session(session["id"])
    if payment is None:
        return

    apply_paid_session(payment, session, source="package_paid")


def handle_async_payment(event):
    session = as_dict(event["data"]["object"])
    if not has_package_metadata(session):
        return

    payment = payment_for_checkout_session(session["id"])
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


def handle_account_updated(event):
    account = as_dict(event["data"]["object"])
    account_id = account.get("id")
    if not account_id:
        return
    StripeAccount.objects.filter(stripe_account_id=account_id).update(
        stripe_details_submitted=account.get("details_submitted", False),
        charges_enabled=account.get("charges_enabled", False),
        payouts_enabled=account.get("payouts_enabled", False),
    )
    if posthog_client is None:
        return
    row = StripeAccount.objects.filter(stripe_account_id=account_id).select_related("user").first()
    if row is not None and row.is_active:
        posthog_client.capture(
            "stripe_onboarding_completed",
            distinct_id=str(row.user_id),
            properties={"duration_seconds": int((timezone.now() - row.created_at).total_seconds())},
        )


def handle_payment_failure(event):
    obj = as_dict(event["data"]["object"])
    failure_message = obj.get("failure_message") or "unknown reason"

    payment_intent_id = obj.get("payment_intent")
    payment = None
    if payment_intent_id:
        payment = payment_for_payment_intent(payment_intent_id)
    elif event["type"] == "transfer.failed" and obj.get("source_transaction"):
        payment = payment_from_charge(obj["source_transaction"])
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

    if posthog_client is not None:
        # The payer's funnel needs its drop-off step: without it, a failed
        # payment and an abandoned checkout are indistinguishable in PostHog.
        payer_id = getattr(payment, "payer_id", None)
        properties = {
            "package_uuid": str(getattr(payment.package, "uuid", "")),
            "failure_reason": failure_message,
            "stripe_event": event["type"],
            "payer_type": "registered" if payer_id else "external",
            "amount_paid": float(payment.amount_paid or 0),
            "payer_currency": payment.payer_currency,
        }
        if payer_id:
            posthog_client.capture(
                "reimbursement_payment_failed",
                distinct_id=str(payer_id),
                properties=properties,
            )
        else:
            posthog_client.capture("reimbursement_payment_failed", properties=properties)

    revert_package_payment(
        payment,
        event=event["type"],
        payment_intent=payment_intent_id,
        failure_message=failure_message,
    )


def handle_charge_refunded(event):
    charge = as_dict(event["data"]["object"])
    payment_intent_id = charge.get("payment_intent")
    if not payment_intent_id:
        return

    charge_currency = charge.get("currency", "usd")
    amount_refunded_cents = charge.get("amount_refunded") or 0
    amount_captured_cents = charge.get("amount_captured") or 0

    refunded_display = from_stripe_amount(amount_refunded_cents, charge_currency)

    logger.warning(
        "Charge refunded — payment_intent: %s, amount: %s %s",
        payment_intent_id,
        charge_currency.upper(),
        refunded_display,
    )

    payment = payment_for_payment_intent(payment_intent_id)
    if payment is None:
        logger.warning("No PackagePayment found for refunded payment_intent %s", payment_intent_id)
        return

    is_full_refund = amount_refunded_cents >= amount_captured_cents
    if is_full_refund:
        # The payer already received this money back via the external refund;
        # issuing another would pay out twice. Revert only.
        revert_package_payment(
            payment,
            event="charge_refunded",
            refund=False,
            payment_intent=payment_intent_id,
            amount_refunded_cents=amount_refunded_cents,
            amount_captured_cents=amount_captured_cents,
            is_full_refund=True,
        )
        return

    # Partial refund: log details for tracking without marking the whole payment failed
    audit(
        payment.package,
        "charge_refunded",
        payment_intent=payment_intent_id,
        amount_refunded_cents=amount_refunded_cents,
        amount_captured_cents=amount_captured_cents,
        is_full_refund=False,
    )


def handle_dispute(event):
    dispute = as_dict(event["data"]["object"])
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

    payment = payment_from_charge(charge_id)
    if payment is None:
        logger.warning("No PackagePayment found for disputed charge %s", charge_id)
        return

    if status == "won":
        restore_paid_payment(
            payment,
            event="charge_dispute_won",
            dispute_id=dispute_id,
        )
        return

    if event["type"] == "charge.dispute.closed" and status == "lost":
        # The chargeback has already returned funds to the cardholder; refunding
        # again would pay out twice. Revert without a Stripe refund.
        revert_package_payment(
            payment,
            event="charge_dispute_lost",
            refund=False,
            dispute_id=dispute_id,
            status=status,
        )
        return

    # Non-terminal dispute (created / under_review / evidence submitted):
    audit(payment.package, "charge_dispute_open", dispute_id=dispute_id, status=status)
    logger.critical(
        "Dispute %s open (status=%s) on package %s — package frozen pending resolution",
        dispute_id,
        status,
        payment.package.uuid,
    )


_EVENT_HANDLERS = {
    "checkout.session.completed": handle_checkout_session_completed,
    "checkout.session.async_payment_succeeded": handle_async_payment,
    "checkout.session.async_payment_failed": handle_async_payment,
    "account.updated": handle_account_updated,
    "transfer.failed": handle_payment_failure,
    "charge.failed": handle_payment_failure,
    "charge.refunded": handle_charge_refunded,
    "charge.dispute.created": handle_dispute,
    "charge.dispute.updated": handle_dispute,
    "charge.dispute.closed": handle_dispute,
}

HANDLED_EVENT_TYPES = frozenset(_EVENT_HANDLERS)


@transaction.atomic
def process_stripe_event(event):
    event_data = as_dict(event)
    event_id = event_data.get("id")

    if event_id:
        try:
            ProcessedStripeEvent.objects.create(event_id=event_id)
        except IntegrityError:
            logger.info("Stripe event %s already processed — skipping", event_id)
            return

    handler = _EVENT_HANDLERS.get(event_data.get("type"))
    if handler is not None:
        handler(event_data)


def notify_package_paid(package_pk: int, payer_pk: int | None) -> None:
    from .tasks import send_package_paid_notification_task

    send_package_paid_notification_task.send(package_pk, payer_pk)
