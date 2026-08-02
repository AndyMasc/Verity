"""Thin service layer for record state transitions.

Keeps archive/unarchive logic out of views so it can be reused from
signals, tasks, or management commands without duplicating business rules.
"""

from django.db import transaction

from billing.models import CustomUser as User

from .models import AuditLog, Record

BULK_LIMIT = 200


class BulkLimitExceededError(Exception):
    """Raised when a bulk operation exceeds the maximum allowed size."""


def archive_record(record: Record) -> None:
    """Soft-delete *record* by marking it inactive."""
    record.is_active = False
    record.save(update_fields=["is_active"])


def unarchive_record(record: Record) -> None:
    """Restore a soft-deleted record by setting it active again."""
    record.is_active = True
    record.save(update_fields=["is_active"])


def bulk_toggle_archive(
    record_ids: list[int],
    user: User,
    *,
    archive: bool,
) -> int:
    """Bulk archive or unarchive records for *user*.

    Uses ``QuerySet.update()`` and ``bulk_create()`` to avoid N+1 queries.
    Wraps everything in a single transaction so partial failures roll back.

    Args:
        record_ids: List of record IDs to toggle.
        user: The owning user (scoped for safety).
        archive: ``True`` to archive, ``False`` to unarchive.

    Returns:
        Number of records affected.

    Raises:
        BulkLimitExceededError: If *record_ids* contains more than ``BULK_LIMIT`` IDs.
    """
    if len(record_ids) > BULK_LIMIT:
        raise BulkLimitExceededError(
            f"Bulk operations are limited to {BULK_LIMIT} records. Received {len(record_ids)}."
        )

    action = AuditLog.Action.ARCHIVE if archive else AuditLog.Action.UNARCHIVE

    with transaction.atomic():
        records = list(
            Record.objects.filter(
                id__in=record_ids,
                user=user,
                is_active=archive,
            )
        )
        if not records:
            return 0

        record_ids_found = [r.id for r in records]
        Record.objects.filter(id__in=record_ids_found).update(is_active=not archive)

        AuditLog.objects.bulk_create(
            [AuditLog(user=user, action=action, record=record) for record in records]
        )

    return len(records)
