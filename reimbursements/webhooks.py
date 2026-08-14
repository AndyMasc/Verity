import logging
from typing import Any

import djstripe.signals as djstripe_signals
import stripe
from django.db import transaction
from django.dispatch import receiver

from records.models import AuditLog

from . import services
from .models import PackagePayment, ProcessedStripeEvent, ReimbursementPackage, StripeAccount

logger = logging.getLogger(__name__)

# Only these event types feed the reimbursements pipeline; everything else
# (e.g. customer.subscription.*, invoice.*, customer.created) is already
# handled by djstripe's own sync and does not need a Dramatiq task.
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
    """Hands the Stripe event to the reimbursements pipeline as a Dramatiq task.

    Runs after djstripe has already synced the event. Enqueued via
    "on_commit" so the worker never races the trigger row's transaction, and
    failures are retried by Dramatiq instead of blocking djstripe's processing.
    """
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    event_type = getattr(getattr(trigger, "event", None), "type", "")
    if event_type not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    transaction.on_commit(lambda: process_stripe_event_task.send(trigger.id))


@receiver(djstripe_signals.webhook_processing_error)
def enqueue_reimbursement_processing_on_error(**kwargs: Any) -> None:
    """Hand the event to the reimbursements pipeline even when djstripe's sync fails.

    djstripe synchronously syncs the event's object graph while processing
    (e.g. the CheckoutSession's PaymentMethod for checkout.session.completed),
    which can fail on transient Stripe API races. When that happens the
    "webhook_post_process" signal never fires, so without this the event would
    be silently dropped and the package would stay stuck at "awaiting payment".
    Processing the raw payload here keeps the reimbursements pipeline
    independent of djstripe's object sync; the task's idempotency marker makes
    duplicate deliveries harmless.
    """
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    if _trigger_event_type(trigger) not in HANDLED_EVENT_TYPES:
        return

    from .tasks import process_stripe_event_task

    # The error signal fires after djstripe's atomic block has rolled back, so
    # there is no transaction to defer to; the trigger row already exists.
    process_stripe_event_task.send(trigger.id)


def _trigger_event_type(trigger: Any) -> str:
    """Best-effort event type for a trigger, from its raw payload or synced Event.

    The raw "json_body" is authoritative and read first: on the processing
    error path djstripe's sync (which populates the Event relation) may have
    already rolled back.
    """
    try:
        event_type = (trigger.json_body or {}).get("type", "")
        if event_type:
            return event_type
    except Exception:
        logger.debug("Unable to read event type from webhook JSON body", exc_info=True)
    return getattr(getattr(trigger, "event", None), "type", "")


def _as_dict(obj):
    """Normalizes webhook payload objects (dict or stripe.StripeObject)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return dict(obj)


def apply_paid_session(payment, session, *, source: str) -> bool:
    """Record a settled Checkout Session against a payment and its package.

    Accepts the session as either a dict or a stripe.StripeObject. Returns
    False without changing any state when the settled amount does not match
    the expected amount (the caller logs context). On success, completes the
    payment, marks the package paid, writes an audit log, and enqueues the
    paid notification. "mark_as_paid" is row-locked and status-guarded, so a
    payment that was already applied (e.g. by the webhook) is not applied
    twice and no duplicate notification is sent.
    """
    session_data = _as_dict(session)
    if not payment.amount_matches(session_data):
        return False

    already_completed = payment.is_completed
    payment.complete_from_session(session_data)
    package = payment.package
    payer_currency = getattr(payment, "payer_currency", None) or "usd"
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


def _payment_for_payment_intent(payment_intent_id: str):
    """Resolve a package payment from a Stripe PaymentIntent id.

    Matches the stored "stripe_payment_intent_id" first. When the completed
    event hasn't been processed yet (the payment row has no PaymentIntent id
    stored), falls back to the PaymentIntent's "package_uuid" metadata — set
    on the session's "payment_intent_data" at checkout creation — so reversal
    events are never missed because they arrived before
    "checkout.session.completed". Raises on transient Stripe errors so the
    task queue retries.
    """
    payment = (
        PackagePayment.objects.select_related("package", "payer")
        .filter(stripe_payment_intent_id=payment_intent_id)
        .first()
    )
    if payment is not None:
        return payment

    try:
        payment_intent = _as_dict(services.retrieve_payment_intent(payment_intent_id))
    except stripe.error.InvalidRequestError:
        logger.error("PaymentIntent %s unknown to Stripe — skipping", payment_intent_id)
        return None
    except stripe.error.StripeError:
        raise

    package_uuid = dict(payment_intent.get("metadata") or {}).get("package_uuid")
    if not package_uuid:
        return None

    payment = (
        PackagePayment.objects.select_related("package", "payer")
        .filter(
            package__uuid=package_uuid,
            stripe_payment_intent_id__isnull=True,
        )
        .order_by("-created_at")
        .first()
    )
    if payment is not None:
        # Remember the id so later events match directly.
        payment.stripe_payment_intent_id = payment_intent_id
        payment.save(update_fields=["stripe_payment_intent_id"])
    return payment


def _payment_from_charge(charge_id: str):
    """Resolve a package payment from a Stripe Charge id via its PaymentIntent."""
    try:
        charge = _as_dict(services.retrieve_charge(charge_id))
    except stripe.error.InvalidRequestError:
        logger.error("Charge %s unknown to Stripe — skipping", charge_id)
        return None
    except stripe.error.StripeError:
        raise

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
    """Restore a payment/package that a won dispute brought back to the platform.

    "mark_as_paid" only acts while the package is open (dispute reversals
    reopen it), and is a no-op otherwise, so re-running is safe.
    """
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


def process_stripe_event(event):
    """Applies a single Stripe webhook event to the reimbursements flow.

    Idempotent: the event id is recorded before any work, so duplicate
    deliveries and retries are no-ops. Runs inside the caller's transaction;
    transient failures raise (so the task queue retries) and permanent
    failures are logged and skipped. Accepts both raw dicts (as produced by
    djstripe's "WebhookEventTrigger.json_body") and stripe.StripeObject
    payloads.
    """
    event_id = event.get("id")
    if event_id:
        _, created = ProcessedStripeEvent.objects.get_or_create(event_id=event_id)
        if not created:
            logger.info("Stripe event %s already processed — skipping", event_id)
            return

    if event["type"] == "checkout.session.completed":
        session = _as_dict(event["data"]["object"])

        # Only process when payment has actually settled.
        # For delayed payment methods the session completes first, then
        # async_payment_succeeded fires later.
        if session.get("payment_status") != "paid":
            return

        metadata = dict(session.get("metadata") or {})
        package_uuid = metadata.get("package_uuid")

        if package_uuid:
            try:
                payment = PackagePayment.objects.select_related("package", "payer").get(
                    stripe_checkout_session_id=session["id"]
                )
            except PackagePayment.DoesNotExist:
                # Verify whether this session is known to Stripe at all.
                # If Stripe itself has no record of it (InvalidRequestError), this
                # is a permanently unresolvable session — give up quietly. If the
                # session is valid but our DB row hasn't appeared yet (race
                # condition), re-raise so the task queue retries.
                try:
                    services.retrieve_checkout_session(session["id"])
                except stripe.error.InvalidRequestError:
                    logger.error(
                        "PackagePayment not found and session %s is unknown to Stripe — skipping",
                        session["id"],
                    )
                    return
                except stripe.error.StripeError:
                    pass  # Network/API issue — fall through to retry below
                logger.error(
                    "PackagePayment not found for session %s — raising for retry",
                    session["id"],
                )
                raise

            if not apply_paid_session(payment, session, source="package_paid"):
                logger.error(
                    "Package %s: session %s amount check failed — skipping mark-as-paid",
                    package_uuid,
                    session["id"],
                )
                return

    elif event["type"] in (
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    ):
        session = _as_dict(event["data"]["object"])
        metadata = dict(session.get("metadata") or {})
        package_uuid = metadata.get("package_uuid")
        if not package_uuid:
            return
        try:
            payment = PackagePayment.objects.select_related("package", "payer").get(
                stripe_checkout_session_id=session["id"]
            )
        except PackagePayment.DoesNotExist:
            return

        if event["type"] == "checkout.session.async_payment_succeeded":
            if not apply_paid_session(payment, session, source="async_payment_succeeded"):
                logger.error(
                    "Package %s: session %s amount check failed — skipping mark-as-paid",
                    package_uuid,
                    session["id"],
                )
                return
        else:
            payment.mark_failed()
            logger.warning(
                "Async payment failed for session %s (package %s)",
                session["id"],
                package_uuid,
            )

    elif event["type"] == "account.updated":
        account = _as_dict(event["data"]["object"])
        StripeAccount.objects.filter(stripe_account_id=account["id"]).update(
            stripe_details_submitted=account.get("details_submitted", False),
            charges_enabled=account.get("charges_enabled", False),
            payouts_enabled=account.get("payouts_enabled", False),
        )

    elif event["type"] in ("transfer.failed", "charge.failed"):
        obj = _as_dict(event["data"]["object"])
        failure_message = obj.get("failure_message") or "unknown reason"
        payment_intent_id = obj.get("payment_intent")
        payment = None
        if payment_intent_id:
            payment = _payment_for_payment_intent(payment_intent_id)
        elif event["type"] == "transfer.failed":
            # Destination-charge transfers carry no payment_intent on the
            # Transfer object; resolve it via the source charge so the
            # package payment can be found and reverted.
            source_transaction = obj.get("source_transaction")
            if source_transaction:
                payment = _payment_from_charge(source_transaction)
                if payment is not None and payment.stripe_payment_intent_id:
                    payment_intent_id = payment.stripe_payment_intent_id
        logger.error(
            "Stripe %s — payment_intent: %s, reason: %s",
            event["type"],
            payment_intent_id,
            failure_message,
        )
        if payment:
            _revert_package_payment(
                payment,
                event=event["type"],
                payment_intent=payment_intent_id,
                failure_message=failure_message,
            )
        else:
            logger.warning(
                "No PackagePayment found for %s (payment_intent: %s)",
                event["type"],
                payment_intent_id,
            )

    elif event["type"] == "charge.refunded":
        charge = _as_dict(event["data"]["object"])
        payment_intent_id = charge.get("payment_intent")
        charge_currency = charge.get("currency") or "usd"
        amount_refunded_cents = charge.get("amount_refunded") or 0
        amount_captured_cents = charge.get("amount_captured") or 0
        if charge_currency.lower() in ("jpy", "krw", "vnd", "idr", "clp", "ugx"):
            refunded_display = float(amount_refunded_cents)
        else:
            refunded_display = amount_refunded_cents / 100
        logger.warning(
            "Charge refunded — payment_intent: %s, amount: %s %.2f",
            payment_intent_id,
            charge_currency.upper(),
            refunded_display,
        )
        if payment_intent_id:
            payment = _payment_for_payment_intent(payment_intent_id)
            if payment:
                # Only fully revert the package if the entire charge was refunded.
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
                else:
                    payment.mark_failed()
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
            else:
                logger.warning(
                    "No PackagePayment found for refunded payment_intent %s",
                    payment_intent_id,
                )

    elif event["type"] in (
        "charge.dispute.created",
        "charge.dispute.updated",
        "charge.dispute.closed",
    ):
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
            # The platform kept the funds; put the package back to paid.
            _restore_paid_payment(
                payment,
                event="charge_dispute_won",
                dispute_id=dispute_id,
            )
        else:
            # Created / updated / closed-lost: funds are (or remain) withdrawn.
            _revert_package_payment(
                payment,
                event="charge_dispute",
                dispute_id=dispute_id,
                status=status,
            )


def _notify_package_paid(package_pk: int, payer_pk: int | None) -> None:
    from .tasks import send_package_paid_notification_task

    send_package_paid_notification_task.send(package_pk, payer_pk)
