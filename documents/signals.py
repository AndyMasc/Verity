"""Django signals for document lifecycle bookkeeping.

R2 cleanup: when a DocumentData row is deleted, the associated R2 file is
removed asynchronously on transaction commit.

Storage accounting: keeps CustomUser.storage_used_bytes in sync with the
document set. pre_save snapshots the previously counted bytes, post_save
applies the delta, and post_delete subtracts the deleted document's
contribution. "QuerySet.delete()" sends these per object, so bulk cleanup
stays accurate too.
"""

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from billing.storage import adjust_storage_usage

from . import tasks
from .models import DocumentData

_COUNTED_FIELDS = frozenset({"file_size"})


@receiver(pre_save, sender=DocumentData)
def snapshot_storage_state(sender, instance, **kwargs):
    """Record the document's previously counted bytes before the save."""
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not (update_fields & _COUNTED_FIELDS):
        instance._storage_previous_counted = instance.file_size or 0
        return

    if not instance.pk:
        instance._storage_previous_counted = 0
        return

    old_size = (
        sender.objects.filter(pk=instance.pk)
        .values_list("file_size", flat=True)
        .first()
    )
    instance._storage_previous_counted = old_size or 0


@receiver(post_save, sender=DocumentData)
def sync_storage_counter(sender, instance, **kwargs):  # noqa: ARG001
    """Apply the document's storage contribution delta to the user counter."""
    previous = getattr(instance, "_storage_previous_counted", 0)
    delta = (instance.file_size or 0) - previous
    if delta:
        adjust_storage_usage(instance.user_id, delta)


@receiver(post_delete, sender=DocumentData)
def remove_storage_counter(sender, instance, **kwargs):  # noqa: ARG001
    """Subtract a permanently deleted document's storage contribution."""
    counted = instance.file_size or 0
    if counted:
        adjust_storage_usage(instance.user_id, -counted)


@receiver(post_delete, sender=DocumentData)
def post_delete_document(sender, instance, **kwargs):  # noqa: ARG001
    """Queue R2 file deletion after the database commit succeeds."""
    if instance.filepath:
        transaction.on_commit(lambda: tasks.delete_document.send(instance.filepath))
