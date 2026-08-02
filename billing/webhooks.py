import logging
from typing import Any

import djstripe.signals as djstripe_signals
from django.db import transaction
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Only these event types feed the reimbursements pipeline; everything else
# (e.g. customer.subscription.*, invoice.*, customer.created) is already
# handled by djstripe's own sync and does not need a QStash task.
HANDLED_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "account.updated",
        "transfer.failed",
        "charge.failed",
        "charge.refunded",
    }
)


@receiver(djstripe_signals.webhook_post_process)
def enqueue_reimbursement_processing(**kwargs: Any) -> None:
    """Hands the Stripe event to the reimbursements pipeline as a QStash task.

    Runs after djstripe has already synced the event. Enqueued via
    ``on_commit`` so the worker never races the trigger row's transaction, and
    failures are retried by QStash instead of blocking djstripe's processing.
    """
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    event_type = getattr(getattr(trigger, "event", None), "type", "")
    if event_type not in HANDLED_EVENT_TYPES:
        return

    from reimbursements.tasks import process_stripe_event_task

    transaction.on_commit(lambda: process_stripe_event_task.delay(trigger.id))


@receiver(djstripe_signals.webhook_processing_error)
def report_webhook_processing_error(**kwargs: Any) -> None:
    """Logs (and surfaces to Sentry) djstripe webhook processing failures."""
    trigger = kwargs.get("instance")
    exception = kwargs.get("exception")
    logger.error(
        "djstripe webhook processing failed (trigger=%s): %s",
        trigger,
        exception,
        exc_info=exception,
    )
