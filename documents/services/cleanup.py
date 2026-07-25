"""Document cleanup service for bulk deletion, orphan removal, and reconciliation.

Encapsulates the business logic for document lifecycle cleanup: batch DB + R2
deletion, orphaned document removal after grace periods, stale upload
reconciliation, and 7-year compliance hard-deletion.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from documents.storage import BUCKET, delete_r2_objects_batch, get_s3_client

logger = logging.getLogger(__name__)

COMPLIANCE_RETENTION_YEARS = 7


def normalize_s3_key(filepath: str) -> str:
    """Strip leading slashes from S3 keys to prevent double-slash paths."""
    return filepath.lstrip("/") if filepath else ""


def bulk_delete_documents(file_data: list[tuple[int, str]]) -> None:
    """Delete documents from both the database and R2 in chunks of 1000.

    DB records are deleted first; R2 cleanup is best-effort and logged on failure.
    """
    CHUNK_SIZE = 1000

    for i in range(0, len(file_data), CHUNK_SIZE):
        chunk = file_data[i : i + CHUNK_SIZE]
        chunk_ids = [item[0] for item in chunk]
        chunk_paths = [normalize_s3_key(item[1]) for item in chunk if item[1]]

        if not chunk_ids:
            continue

        try:
            DocumentData.objects.filter(id__in=chunk_ids).delete()
        except Exception as e:
            logger.error("Failed to delete orphaned DB records: %s", e, exc_info=True)
            continue

        if not chunk_paths:
            continue

        try:
            s3 = get_s3_client()
            s3.delete_objects(
                Bucket=BUCKET,
                Delete={"Objects": [{"Key": path} for path in chunk_paths]},
            )
        except Exception as e:
            logger.error(
                "R2 cleanup failed for orphaned keys (DB already cleaned): %s",
                e,
                exc_info=True,
            )
            continue


def delete_orphaned_documents() -> None:
    """Remove unlinked documents after a grace period.

    Non-OCR documents older than 1 day and unassociated OCR documents older
    than 7 days are hard-deleted from both DB and R2.
    """
    grace_period = timezone.now() - timedelta(days=1)
    orphaned_files = DocumentData.objects.filter(
        associated_record=None,
        deleted_at__isnull=True,
        date_added__lt=grace_period,
        did_ocr=False,
    ).exclude(status=DocumentStatus.DELETING)

    if orphaned_files.exists():
        file_data = list(orphaned_files.values_list("id", "filepath"))
        bulk_delete_documents(file_data)
        logger.info("Orphaned documents cleanup completed.")

    ocr_grace = timezone.now() - timedelta(days=7)
    abandoned_ocr = DocumentData.objects.filter(
        associated_record=None,
        deleted_at__isnull=True,
        date_added__lt=ocr_grace,
        did_ocr=True,
        status__in=[
            DocumentStatus.UPLOADED,
            DocumentStatus.PROCESSING,
            DocumentStatus.COMPLETED,
            DocumentStatus.ERROR,
        ],
    )

    if abandoned_ocr.exists():
        file_data = list(abandoned_ocr.values_list("id", "filepath"))
        bulk_delete_documents(file_data)
        logger.info("Abandoned OCR documents cleanup completed.")


def reconcile_documents() -> None:
    """Clean up stale pending uploads and dangling error records.

    Removes pending uploads older than 30 minutes and errored documents
    older than 2 days, deleting both the R2 objects and database records.
    """
    s3 = get_s3_client()
    stale_cutoff = timezone.now() - timedelta(minutes=30)
    abandoned_uploads = DocumentData.objects.filter(
        filepath__isnull=False,
        deleted_at__isnull=True,
        status=DocumentStatus.PENDING_UPLOAD,
        date_added__lt=stale_cutoff,
    )

    deleted_ids = []
    for doc in abandoned_uploads.iterator(chunk_size=200):
        if not doc.filepath:
            continue
        try:
            s3.delete_object(Bucket=BUCKET, Key=normalize_s3_key(doc.filepath))
            deleted_ids.append(doc.id)
        except Exception as e:
            logger.error("Failed cleanup of object storage for upload %s: %s", doc.id, e)
            continue

    if deleted_ids:
        DocumentData.objects.filter(id__in=deleted_ids).delete()
        logger.info("Reconciliation: cleaned up %d stale pending uploads.", len(deleted_ids))

    dangling_records = DocumentData.objects.filter(
        deleted_at__isnull=True,
        status=DocumentStatus.ERROR,
        date_added__lt=timezone.now() - timedelta(days=2),
    )
    dangling_ids = list(dangling_records.values_list("id", "filepath"))
    if dangling_ids:
        dangling_paths = [path for _, path in dangling_ids if path]
        if dangling_paths:
            delete_r2_objects_batch(dangling_paths)
        DocumentData.objects.filter(id__in=[d[0] for d in dangling_ids]).delete()
        logger.info("Reconciliation: removed %d dangling error records.", len(dangling_ids))


def delete_7year_deleted_documents() -> None:
    """Hard-delete documents soft-deleted over 7 years ago for users with auto-delete enabled.

    Enforces financial record retention policy by permanently removing
    documents that have exceeded the compliance window.
    """
    seven_years_ago = timezone.now() - timedelta(days=365 * COMPLIANCE_RETENTION_YEARS)
    expired_deleted = DocumentData.objects.filter(
        deleted_at__isnull=False,
        date_added__lte=seven_years_ago,
        user__settings__auto_delete_deleted_documents=True,
    )
    count = expired_deleted.count()
    if not count:
        return

    file_data = list(expired_deleted.values_list("id", "filepath"))
    bulk_delete_documents(file_data)
    logger.info("Hard-deleted %d documents soft-deleted for 7+ years.", count)
