"""Django signals for automatic UserSettings provisioning and cache invalidation.

Listens for new User creation and ensures every user starts with a
sensible set of default preferences. Also invalidates the webpush
subscription count cache and the dashboard context cache when the
underlying data changes.
"""

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from core.services.dashboard import invalidate_dashboard_cache
from Verity.utils import bump_paginator_count_version

from .models import UserSettings


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_settings(sender, instance, created, **kwargs):  # noqa: ARG001
    """Create a default UserSettings row whenever a new User is saved."""
    if created:
        UserSettings.objects.create(user=instance)


def _invalidate_webpush_count_cache(user_id):
    """Remove the cached webpush subscription count for *user_id*."""
    cache.delete(f"webpush_count:{user_id}")


@receiver(post_save, sender="webpush.PushInformation")
def _on_pushinfo_save(sender, instance, **kwargs):  # noqa: ARG001
    _invalidate_webpush_count_cache(instance.user_id)


@receiver(post_delete, sender="webpush.PushInformation")
def _on_pushinfo_delete(sender, instance, **kwargs):  # noqa: ARG001
    _invalidate_webpush_count_cache(instance.user_id)


def _invalidate_dashboard_for(user_ids) -> None:
    """Invalidate the dashboard cache for any non-null user ids."""
    for user_id in user_ids:
        if user_id is not None:
            invalidate_dashboard_cache(user_id)


@receiver(post_save, sender="records.Record")
@receiver(post_delete, sender="records.Record")
def _on_record_change(sender, instance, **kwargs):  # noqa: ARG001
    invalidate_dashboard_cache(instance.user_id)
    bump_paginator_count_version("record", instance.user_id)


@receiver(post_save, sender="documents.DocumentData")
@receiver(post_delete, sender="documents.DocumentData")
def _on_document_change(sender, instance, **kwargs):  # noqa: ARG001
    invalidate_dashboard_cache(instance.user_id)
    bump_paginator_count_version("documentdata", instance.user_id)


@receiver(post_save, sender="core.Notification")
@receiver(post_delete, sender="core.Notification")
def _on_notification_change(sender, instance, **kwargs):  # noqa: ARG001
    invalidate_dashboard_cache(instance.recipient_id)


@receiver(post_save, sender="core.UserSettings")
def _on_user_settings_change(sender, instance, **kwargs):  # noqa: ARG001
    invalidate_dashboard_cache(instance.user_id)


@receiver(post_save, sender="records.MergeLog")
@receiver(post_delete, sender="records.MergeLog")
def _on_merge_log_change(sender, instance, **kwargs):  # noqa: ARG001
    plaid_record = instance.plaid_record
    if plaid_record is not None:
        invalidate_dashboard_cache(plaid_record.user_id)


@receiver(post_save, sender="reimbursements.ReimbursementPackage")
@receiver(post_delete, sender="reimbursements.ReimbursementPackage")
def _on_reimbursement_package_change(sender, instance, **kwargs):  # noqa: ARG001
    _invalidate_dashboard_for((instance.creator_id, instance.recipient_id))


@receiver(post_save, sender="reimbursements.PackagePayment")
@receiver(post_delete, sender="reimbursements.PackagePayment")
def _on_package_payment_change(sender, instance, **kwargs):  # noqa: ARG001
    from reimbursements.models import PackagePayment

    row = (
        PackagePayment.objects.filter(pk=instance.pk)
        .values_list("package__creator_id", "package__recipient_id")
        .first()
    )
    _invalidate_dashboard_for(row or ())
