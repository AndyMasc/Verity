from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from . import tasks
from .models import DocumentData


@receiver(post_delete, sender=DocumentData)
def post_delete_document(sender, instance, **kwargs):  # noqa: ARG001
    """Queue R2 file deletion after the database commit succeeds."""
    if instance.filepath:
        transaction.on_commit(lambda: tasks.delete_document.send(instance.filepath))
