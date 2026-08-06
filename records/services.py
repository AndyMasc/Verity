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


def archive_record(user: User, record: Record) -> None:
    """Soft-delete *record* by marking it inactive and logging the action."""
    with transaction.atomic():
        record.is_active = False
        record.save(update_fields=["is_active"])
        AuditLog.objects.create(user=user, action=AuditLog.Action.ARCHIVE, record=record)


def unarchive_record(user: User, record: Record) -> None:
    """Restore a soft-deleted record and log the action."""
    with transaction.atomic():
        record.is_active = True
        record.save(update_fields=["is_active"])
        AuditLog.objects.create(user=user, action=AuditLog.Action.UNARCHIVE, record=record)


def soft_delete_record(user: User, record: Record) -> None:
    """Soft-delete *record* and log the action."""
    with transaction.atomic():
        record.delete()
        AuditLog.objects.create(user=user, action=AuditLog.Action.SOFT_DELETE, record=record)


def hard_delete_record(user: User, record: Record) -> None:
    """Permanently delete *record* along with its associated documents.

    Associated ``DocumentData`` rows (and their R2 objects via signals) are
    hard-deleted before the record itself, all in one transaction. The caller
    is responsible for age checks and rate limiting.
    """
    from documents.models import DocumentData

    with transaction.atomic():
        for doc in DocumentData.objects.filter(associated_record=record):
            doc.hard_delete()
        AuditLog.objects.create(
            user=user,
            action=AuditLog.Action.HARD_DELETE,
            record=record,
            details={"title": record.title},
        )
        record.hard_delete()


def kickoff_ocr_scan(user: User, document) -> str | None:
    """Kick off OCR extraction for *document*, or return a user-facing warning.

    Guards on the monthly scan limit: when the limit is reached the document is
    marked ERROR and a message is returned for the view to surface. Returns
    ``None`` when extraction was kicked off (or is already in flight).
    """
    from django.core.cache import cache

    from billing import entitlements
    from documents.models import DocumentStatus
    from documents.services.ocr import set_document_status
    from documents.tasks import extract_document

    cache_key = f"ocr_status_{document.id}"
    if cache.get(cache_key) is not None:
        return None

    if not entitlements.can_scan(user):
        message = (
            "Quick Scan limit reached. Upgrade to Papertrail Pro for "
            "unlimited Quick Scans, or enter the details manually."
        )
        cache.set(cache_key, {"error": message}, timeout=600)
        set_document_status(document.id, DocumentStatus.ERROR, ocr_error="scan_limit_reached")
        return message

    if document.did_ocr:
        document.did_ocr = False
        document.save(update_fields=["did_ocr"])
    cache.set(cache_key, "processing", timeout=600)
    entitlements.record_scan(user)
    extract_document.delay(document.id)
    return None


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
