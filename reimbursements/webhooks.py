import logging

import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from records.models import AuditLog

from .models import PackagePayment, StripeAccount

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
@ratelimit(key="ip", rate="60/m", method="POST", block=True)
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except ValueError:
        logger.error("Stripe webhook: invalid payload — could not parse")
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        logger.error("Stripe webhook: invalid signature (possible tampering or config mismatch)")
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]

        # Only process when payment has actually settled.
        # For delayed payment methods the session completes first, then
        # async_payment_succeeded fires later.
        if session.get("payment_status") != "paid":
            return HttpResponse(status=200)

        package_uuid = session.get("metadata", {}).get("package_uuid")

        if package_uuid:
            try:
                payment = PackagePayment.objects.select_related("package", "payer").get(
                    stripe_checkout_session_id=session["id"]
                )
            except PackagePayment.DoesNotExist:
                # Verify whether this session is known to Stripe at all.
                # If Stripe itself has no record of it (InvalidRequestError), this
                # is a permanently unresolvable session — return 400 so Stripe
                # stops retrying.  If the session is valid but our DB row hasn't
                # appeared yet (race condition), return 500 to request a retry.
                try:
                    stripe.checkout.Session.retrieve(session["id"])
                except stripe.error.InvalidRequestError:
                    logger.error(
                        "PackagePayment not found and session %s is unknown to Stripe — returning 400",
                        session["id"],
                    )
                    return HttpResponse(status=400)
                except stripe.error.StripeError:
                    pass  # Network/API issue — fall through to 500 retry below
                logger.error(
                    "PackagePayment not found for session %s — returning 500 for Stripe retry",
                    session["id"],
                )
                return HttpResponse(status=500)

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

            from .tasks import send_package_paid_notification_task

            send_package_paid_notification_task.delay(package.pk, payment.payer.pk)

    elif event["type"] in (
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
    ):
        session = event["data"]["object"]
        package_uuid = session.get("metadata", {}).get("package_uuid")
        if not package_uuid:
            return HttpResponse(status=200)
        try:
            payment = PackagePayment.objects.select_related("package", "payer").get(
                stripe_checkout_session_id=session["id"]
            )
        except PackagePayment.DoesNotExist:
            return HttpResponse(status=200)

        if event["type"] == "checkout.session.async_payment_succeeded":
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
            from .tasks import send_package_paid_notification_task

            send_package_paid_notification_task.delay(package.pk, payment.payer.pk)
        else:
            payment.is_completed = False
            payment.save(update_fields=["is_completed"])
            logger.warning(
                "Async payment failed for session %s (package %s)",
                session["id"],
                package_uuid,
            )

    elif event["type"] == "account.updated":
        account = event["data"]["object"]
        StripeAccount.objects.filter(stripe_account_id=account["id"]).update(
            stripe_details_submitted=account.get("details_submitted", False),
            charges_enabled=account.get("charges_enabled", False),
            payouts_enabled=account.get("payouts_enabled", False),
        )

    elif event["type"] in ("transfer.failed", "charge.failed"):
        obj = event["data"]["object"]
        failure_message = obj.get("failure_message", "unknown reason")
        payment_intent_id = obj.get("payment_intent") or obj.get("id")
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
        charge = event["data"]["object"]
        payment_intent_id = charge.get("payment_intent")
        charge_currency = charge.get("currency", "usd")
        amount_refunded_cents = charge.get("amount_refunded", 0)
        amount_captured_cents = charge.get("amount_captured", 0)
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

    return HttpResponse(status=200)
