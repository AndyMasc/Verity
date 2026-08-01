import logging

import djstripe.signals as djstripe_signals
from django.db import transaction
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(djstripe_signals.webhook_post_process)
def enqueue_reimbursement_processing(sender, **kwargs):
    """Hands the Stripe event to the reimbursements pipeline as a QStash task.

    Runs after djstripe has already synced the event. Enqueued via
    ``on_commit`` so the worker never races the trigger row's transaction, and
    failures are retried by QStash instead of blocking djstripe's processing.
    """
    trigger = kwargs.get("instance")
    if trigger is None:
        return

    from reimbursements.tasks import process_stripe_event_task

    transaction.on_commit(lambda: process_stripe_event_task.delay(trigger.id))


@receiver(djstripe_signals.webhook_processing_error)
def report_webhook_processing_error(sender, **kwargs):
    """Logs (and surfaces to Sentry) djstripe webhook processing failures."""
    trigger = kwargs.get("instance")
    exception = kwargs.get("exception")
    logger.error(
        "djstripe webhook processing failed (trigger=%s): %s",
        trigger,
        exception,
        exc_info=exception,
    )
