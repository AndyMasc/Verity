"""Django signals for the records module.

Hooks into "post_save" on Record to trigger automatic matching with
Plaid transactions whenever a record is updated (not created). The
matching task is enqueued inside "transaction.on_commit" to avoid
race conditions with uncommitted data.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from records.models import Record

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Record)
def auto_match_on_record_save(sender, instance, created, **kwargs):  # noqa: ARG001
    if created:
        return
    if not instance.is_active:
        return
    if getattr(instance, "_skip_auto_match", False):
        return

    transaction.on_commit(lambda: _enqueue_auto_match(instance))


def _enqueue_auto_match(instance: Record) -> None:
    from records.tasks import run_auto_match

    run_auto_match.send(instance.pk, has_plaid=bool(instance.plaid_transaction_id))
