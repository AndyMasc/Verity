import logging

import dramatiq
import stripe
from django.db import transaction
from periodiq import cron

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
    """Reconcile a single pending payment against Stripe.

    Triggered from page views while a package is still open. Uses the same
    amount cross-check, audit log, and paid notification as the webhook path
    so a payment that settled without a processed webhook is recorded
    identically.
    """
    try:
        package = ReimbursementPackage.objects.get(uuid=package_uuid)
    except ReimbursementPackage.DoesNotExist:
        logger.warning("sync_payment_status: package %s not found", package_uuid)
        return

    if package.status != ReimbursementPackage.Status.OPEN:
        return

    try:
        payment = PackagePayment.objects.select_related("package", "payer").get(
            pk=payment_id, package=package
        )
    except PackagePayment.DoesNotExist:
        return

    if _sync_payment_from_stripe(payment, source="payment_synced"):
        logger.info("Background sync: marked package %s as paid", package_uuid)


def _sync_payment_from_stripe(payment, *, source: str) -> bool:
    """Fetch a payment's Checkout Session and apply it when settled.

    Returns True when the payment was settled and applied. Raises on Stripe
    API failure so the caller can trigger a retry.
    """
    try:
        session = services.retrieve_checkout_session(payment.stripe_checkout_session_id)
    except stripe.error.StripeError as e:
        logger.warning(
            "sync_payment_status: failed to retrieve session %s — %s",
            payment.stripe_checkout_session_id,
            e,
        )
        raise

    if session.payment_status != "paid":
        return False

    from .webhooks import apply_paid_session

    if not apply_paid_session(payment, session, source=source):
        logger.error(
            "Package %s: session %s amount check failed — skipping mark-as-paid",
            payment.package.uuid,
            session.id,
        )
        return False
    return True


@dramatiq.actor(max_retries=3, min_backoff=2, periodic=cron("*/15 * * * *"))
def reconcile_pending_payments_task() -> None:
    """Periodically reconcile open packages' pending payments against Stripe.

    A webhook delivery can be missed or dropped (djstripe's object sync
    failing on a transient Stripe race, a downed worker, Stripe giving up
    after failed retries), leaving an open package that was actually paid
    stuck at "awaiting payment". This task refetches the Checkout Session for
    each pending payment so recorded state never trusts stale local data.
    Per-payment failures are logged and skipped so one bad session doesn't
    block the rest.
    """
    package_ids = (
        ReimbursementPackage.objects.filter(
            status=ReimbursementPackage.Status.OPEN,
            payments__is_completed=False,
        )
        .values_list("pk", flat=True)
        .distinct()
    )

    for package_id in package_ids:
        payment = (
            PackagePayment.objects.select_related("package", "payer")
            .filter(package_id=package_id, is_completed=False)
            .order_by("-created_at")
            .first()
        )
        if payment is None:
            continue
        try:
            if _sync_payment_from_stripe(payment, source="payment_synced"):
                logger.info(
                    "Reconciliation: marked package %s as paid",
                    payment.package.uuid,
                )
        except Exception:
            logger.exception(
                "Reconciliation failed for package %s",
                package_id,
            )


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
