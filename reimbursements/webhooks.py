import logging
from decimal import Decimal

import stripe
from django.conf import settings
from django.db import transaction

from core.currencies import from_stripe_amount
from records.models import AuditLog

from .models import PackagePayment, ProcessedStripeEvent, StripeAccount

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


def _as_dict(obj):
    """Normalizes webhook payload objects (dict or stripe.StripeObject)."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict_recursive"):
        return obj.to_dict_recursive()
    return dict(obj)


def _session_amount_matches(session: dict, payment: PackagePayment) -> bool:
    """Cross-check the settled checkout amount against the expected package total.

    Prevents a session for a different/edited amount from being treated as a
    completed payment. A small tolerance absorbs the rounding difference
    between Stripe's per-line-item cent rounding and the stored converted
    total.
    """
    session_currency = (session.get("currency") or payment.payer_currency).lower()
    amount_total = session.get("amount_total")
    if session_currency != payment.payer_currency.lower():
        logger.error(
            "Package %s: session %s currency mismatch (%s vs %s) — refusing to mark as paid",
            payment.package_id,
            session.get("id"),
            session_currency,
            payment.payer_currency,
        )
        return False
    if amount_total is None:
        logger.error(
            "Package %s: session %s has no amount_total — refusing to mark as paid",
            payment.package_id,
            session.get("id"),
        )
        return False
    settled = from_stripe_amount(amount_total, session_currency)
    expected = payment.amount_paid
    tolerance = max(Decimal("0.02"), expected * Decimal("0.01"))
    if abs(settled - expected) > tolerance:
        logger.error(
            "Package %s: session %s settled amount %s %s does not match expected %s %s — refusing to mark as paid",
            payment.package_id,
            session.get("id"),
            settled,
            session_currency,
            expected,
            payment.payer_currency,
        )
        return False
    return True


def process_stripe_event(event):
    """Applies a single Stripe webhook event to the reimbursements flow.

    Idempotent: the event id is recorded before any work, so duplicate
    deliveries and retries are no-ops. Runs inside the caller's transaction;
    transient failures raise (so the task queue retries) and permanent
    failures are logged and skipped. Accepts both raw dicts (as produced by
    djstripe's ``WebhookEventTrigger.json_body``) and stripe.StripeObject
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
                    stripe.checkout.Session.retrieve(session["id"])
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

            if not _session_amount_matches(session, payment):
                logger.error(
                    "Package %s: session %s amount check failed — skipping mark-as-paid",
                    package_uuid,
                    session["id"],
                )
                return

            payment.is_completed = True
            payment_intent_id = session.get("payment_intent")
            if payment_intent_id:
                payment.stripe_payment_intent_id = payment_intent_id
            payment.save(update_fields=["is_completed", "stripe_payment_intent_id"])

            package = payment.package
            payer_currency = getattr(payment, "payer_currency", None) or "usd"
            package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)

            AuditLog.objects.create(
                user=package.creator,
                action=AuditLog.Action.UPDATE_RECORD,
                details={
                    "event": "package_paid",
                    "package_uuid": str(package.uuid),
                    "stripe_session_id": session["id"],
                    "payer_email": payment.payer.email if payment.payer else None,
                    "amount": str(payment.amount_paid),
                },
            )

            transaction.on_commit(lambda: _notify_package_paid(package.pk, payment.payer.pk))

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
            if not _session_amount_matches(session, payment):
                logger.error(
                    "Package %s: session %s amount check failed — skipping mark-as-paid",
                    package_uuid,
                    session["id"],
                )
                return
            payment.is_completed = True
            payment_intent_id = session.get("payment_intent")
            if payment_intent_id:
                payment.stripe_payment_intent_id = payment_intent_id
            payment.save(update_fields=["is_completed", "stripe_payment_intent_id"])
            package = payment.package
            payer_currency = getattr(payment, "payer_currency", None) or "usd"
            package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)
            AuditLog.objects.create(
                user=package.creator,
                action=AuditLog.Action.UPDATE_RECORD,
                details={
                    "event": "async_payment_succeeded",
                    "package_uuid": str(package.uuid),
                    "stripe_session_id": session["id"],
                    "payer_email": payment.payer.email if payment.payer else None,
                },
            )
            transaction.on_commit(lambda: _notify_package_paid(package.pk, payment.payer.pk))
        else:
            payment.is_completed = False
            payment.save(update_fields=["is_completed"])
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
        if not payment_intent_id and event["type"] == "transfer.failed":
            # Destination-charge transfers carry no payment_intent on the
            # Transfer object; resolve it via the source charge so the
            # package payment can be found and reverted.
            source_transaction = obj.get("source_transaction")
            if source_transaction:
                try:
                    charge = stripe.Charge.retrieve(source_transaction)
                except stripe.error.StripeError:
                    logger.exception(
                        "Failed to retrieve source charge %s for %s",
                        source_transaction,
                        event["type"],
                    )
                    raise
                payment_intent_id = charge.get("payment_intent")
        logger.error(
            "Stripe %s — payment_intent: %s, reason: %s",
            event["type"],
            payment_intent_id,
            failure_message,
        )
        if payment_intent_id:
            payment = (
                PackagePayment.objects.filter(stripe_payment_intent_id=payment_intent_id)
                .select_related("package")
                .first()
            )
            if payment:
                payment.is_completed = False
                payment.save(update_fields=["is_completed"])
                payment.package.mark_as_refunded()
                AuditLog.objects.create(
                    user=payment.package.creator,
                    action=AuditLog.Action.UPDATE_RECORD,
                    details={
                        "event": event["type"],
                        "package_uuid": str(payment.package.uuid),
                        "payment_intent": payment_intent_id,
                        "failure_message": failure_message,
                    },
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
            payment = (
                PackagePayment.objects.select_related("package__creator")
                .filter(
                    stripe_payment_intent_id=payment_intent_id,
                )
                .first()
            )
            if payment:
                # Only fully revert the package if the entire charge was refunded.
                is_full_refund = amount_refunded_cents >= amount_captured_cents
                payment.is_completed = False
                payment.save(update_fields=["is_completed"])
                if is_full_refund:
                    payment.package.mark_as_refunded()
                AuditLog.objects.create(
                    user=payment.package.creator,
                    action=AuditLog.Action.UPDATE_RECORD,
                    details={
                        "event": "charge_refunded",
                        "package_uuid": str(payment.package.uuid),
                        "payment_intent": payment_intent_id,
                        "amount_refunded_cents": amount_refunded_cents,
                        "amount_captured_cents": amount_captured_cents,
                        "is_full_refund": is_full_refund,
                    },
                )
            else:
                logger.warning(
                    "No PackagePayment found for refunded payment_intent %s",
                    payment_intent_id,
                )


def _notify_package_paid(package_pk: int, payer_pk: int | None) -> None:
    from .tasks import send_package_paid_notification_task

    send_package_paid_notification_task.delay(package_pk, payer_pk)
