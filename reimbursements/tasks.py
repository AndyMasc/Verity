import logging

import dramatiq
from django.db import transaction
from periodiq import cron

from . import services
from .models import PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)


@dramatiq.actor(max_retries=3)
def process_stripe_event_task(trigger_id: int) -> None:
    from djstripe.models import WebhookEventTrigger

    from .webhooks import process_stripe_event

    try:
        trigger = WebhookEventTrigger.objects.get(pk=trigger_id)
    except WebhookEventTrigger.DoesNotExist:
        logger.warning("process_stripe_event_task: trigger %s not found",
                       trigger_id)
        return

    with transaction.atomic():
        process_stripe_event(trigger.json_body)


@dramatiq.actor(max_retries=3)
def sync_payment_status(package_uuid: str, payment_id: int) -> None:
    try:
        package = ReimbursementPackage.objects.get(uuid=package_uuid)
    except ReimbursementPackage.DoesNotExist:
        logger.warning("sync_payment_status: package %s not found",
                       package_uuid)
        return

    if package.status not in (ReimbursementPackage.Status.OPEN,
                              ReimbursementPackage.Status.QUEUED):
        return

    try:
        payment = PackagePayment.objects.select_related(
            "package", "payer").get(pk=payment_id, package=package)
    except PackagePayment.DoesNotExist:
        return

    if sync_payment_from_stripe(payment, source="payment_synced"):
        logger.info("Background sync: marked package %s as paid", package_uuid)


def sync_payment_from_stripe(payment, *, source: str) -> bool:
    """Backstop for a missed webhook: ask Stripe whether this invoice settled.

    Only invoice-raised payments can be outstanding, so a lost webhook
    self-heals on the next sweep.
    """
    from .webhooks import apply_paid_invoice

    if payment.stripe_invoice_id:
        invoice = services.retrieve_invoice(payment.stripe_invoice_id)
        if invoice.get("status") != "paid":
            return False
        settled = {
            "id": invoice.get("id"),
            "amount_paid": invoice.get("amount_paid"),
            "currency": invoice.get("currency"),
            "payment_intent": invoice.get("payment_intent"),
        }
        if not apply_paid_invoice(payment, settled, source=source):
            logger.error(
                "Package %s: invoice %s amount check failed — skipping mark-as-paid",
                payment.package.uuid,
                payment.stripe_invoice_id,
            )
            return False
        return True

    return False


@dramatiq.actor(max_retries=3, min_backoff=2, periodic=cron("*/15 * * * *"))
def reconcile_pending_payments_task() -> None:
    package_ids = (ReimbursementPackage.objects.filter(
        status__in=(ReimbursementPackage.Status.OPEN,
                    ReimbursementPackage.Status.QUEUED),
        payments__is_completed=False,
    ).values_list("pk", flat=True).distinct())

    for package_id in package_ids:
        payment = (PackagePayment.objects.select_related(
            "package", "payer").filter(
                package_id=package_id,
                is_completed=False).order_by("-created_at").first())
        if payment is None:
            continue
        try:
            if sync_payment_from_stripe(payment, source="payment_synced"):
                logger.info(
                    "Reconciliation: marked package %s as paid",
                    payment.package.uuid,
                )
        except Exception:
            logger.exception(
                "Reconciliation failed for package %s",
                package_id,
            )


@dramatiq.actor(max_retries=3, min_backoff=300, periodic=cron("30 4 * * *"))
def daily_stripe_reconciliation_task() -> None:
    from .reconciliation import run_daily_reconciliation

    run_daily_reconciliation()


@dramatiq.actor
def send_package_paid_notification_task(package_pk: int,
                                        payer_pk: int | None) -> None:
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
            logger.warning(
                "Payer %s for package %s no longer exists; sending anonymised notification",
                payer_pk,
                package_pk,
            )

    send_package_paid_notification(package, payer)
