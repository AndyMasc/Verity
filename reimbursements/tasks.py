import logging

import stripe
from django.conf import settings
from django_qstash import shared_task

from .models import PackagePayment, ReimbursementPackage

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


@shared_task
def sync_payment_status(package_uuid: str, payment_id: int) -> None:
    try:
        package = ReimbursementPackage.objects.get(uuid=package_uuid)
    except ReimbursementPackage.DoesNotExist:
        logger.warning("sync_payment_status: package %s not found", package_uuid)
        return

    if package.status != ReimbursementPackage.Status.OPEN:
        return

    try:
        payment = PackagePayment.objects.get(pk=payment_id, package=package)
    except PackagePayment.DoesNotExist:
        return

    try:
        session = stripe.checkout.Session.retrieve(payment.stripe_checkout_session_id)
    except stripe.error.StripeError:
        logger.warning(
            "sync_payment_status: failed to retrieve session %s", payment.stripe_checkout_session_id
        )
        return

    if session.payment_status == "paid":
        payment.is_completed = True
        payment_intent_id = getattr(session, "payment_intent", None)
        if payment_intent_id:
            payment.stripe_payment_intent_id = payment_intent_id
        payment.save(update_fields=["is_completed", "stripe_payment_intent_id"])
        payer_currency = getattr(payment, "payer_currency", None) or "usd"
        package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)
        logger.info("Background sync: marked package %s as paid", package_uuid)


@shared_task
def send_package_paid_notification_task(package_pk: int, payer_pk: int | None) -> None:
    from django.contrib.auth import get_user_model

    from .models import ReimbursementPackage
    from .notifications import send_package_paid_notification

    try:
        package = ReimbursementPackage.objects.get(pk=package_pk)
    except ReimbursementPackage.DoesNotExist:
        return

    payer = None
    if payer_pk:
        try:
            payer = get_user_model().objects.get(pk=payer_pk)
        except get_user_model().DoesNotExist:
            pass

    send_package_paid_notification(package, payer)
