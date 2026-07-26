import logging

import stripe
from django.conf import settings
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from records.models import AuditLog

from .models import PackagePayment, StripeAccount

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def stripe_webhook(request):
    payload = request.body
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return HttpResponse(status=400)
    except stripe.error.SignatureVerificationError:
        return HttpResponse(status=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        package_uuid = session.get("metadata", {}).get("package_uuid")

        if package_uuid:
            try:
                payment = PackagePayment.objects.select_related("package", "payer").get(
                    stripe_checkout_session_id=session["id"]
                )
            except PackagePayment.DoesNotExist:
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
            package.mark_as_paid(payer=payment.payer)

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

            _send_package_paid_notification(package, payment.payer)

    elif event["type"] == "account.updated":
        account = event["data"]["object"]
        if account.get("details_submitted"):
            StripeAccount.objects.filter(stripe_account_id=account["id"]).update(
                stripe_details_submitted=True
            )

    elif event["type"] in ("transfer.failed", "charge.failed"):
        obj = event["data"]["object"]
        failure_message = obj.get("failure_message", "unknown reason")
        payment_intent_id = obj.get("payment_intent", "N/A")
        logger.error(
            "Stripe %s — payment_intent: %s, reason: %s",
            event["type"],
            payment_intent_id,
            failure_message,
        )

    elif event["type"] == "charge.refunded":
        charge = event["data"]["object"]
        payment_intent_id = charge.get("payment_intent")
        amount_refunded = charge.get("amount_refunded", 0) / 100
        logger.warning(
            "Charge refunded — payment_intent: %s, amount: $%.2f",
            payment_intent_id,
            amount_refunded,
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
                payment.package.mark_as_refunded()
                AuditLog.objects.create(
                    user=payment.package.creator,
                    action=AuditLog.Action.UPDATE_RECORD,
                    details={
                        "event": "charge_refunded",
                        "package_uuid": str(payment.package.uuid),
                        "payment_intent": payment_intent_id,
                        "amount_refunded": str(amount_refunded),
                    },
                )
            else:
                logger.warning(
                    "No PackagePayment found for refunded payment_intent %s",
                    payment_intent_id,
                )

    return HttpResponse(status=200)


def _send_package_paid_notification(package, payer) -> None:
    from .notifications import send_package_paid_notification

    send_package_paid_notification(package, payer)
