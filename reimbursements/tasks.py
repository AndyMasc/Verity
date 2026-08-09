import logging

import dramatiq
import stripe
from django.db import transaction

from . import services
from .models import PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)


@dramatiq.actor(max_retries=3)
def process_stripe_event_task(trigger_id: int) -> None:
    """Applies a Stripe webhook event to the reimbursements flow, off the
    request path.

    Enqueued from the djstripe "webhook_post_process" signal. Transient
    failures raise out of "process_stripe_event" so Dramatiq retries; the
    surrounding transaction guarantees the event-id dedupe marker rolls back
    with any partial work.
    """
    from djstripe.models import WebhookEventTrigger

    from .webhooks import process_stripe_event

    try:
        trigger = WebhookEventTrigger.objects.get(pk=trigger_id)
    except WebhookEventTrigger.DoesNotExist:
        logger.warning("process_stripe_event_task: trigger %s not found", trigger_id)
        return

    with transaction.atomic():
        process_stripe_event(trigger.json_body)


@dramatiq.actor(max_retries=3)
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
        session = services.retrieve_checkout_session(payment.stripe_checkout_session_id)
    except stripe.error.StripeError as e:
        logger.warning(
            "sync_payment_status: failed to retrieve session %s — %s",
            payment.stripe_checkout_session_id,
            e,
        )
        raise  # Trigger Dramatiq retry

    if session.payment_status == "paid":
        payment.complete_from_session(session)
        payer_currency = getattr(payment, "payer_currency", None) or "usd"
        package.mark_as_paid(payer=payment.payer, payer_currency=payer_currency)
        logger.info("Background sync: marked package %s as paid", package_uuid)


@dramatiq.actor
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
