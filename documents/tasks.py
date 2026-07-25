"""Background tasks for OCR extraction, document deletion, and storage reconciliation.

Uses django-q-stash for async execution with retry/backoff. Each task is a thin
wrapper that delegates to the corresponding service module.
"""

from typing import Any

from django_qstash import shared_task

from .services.cleanup import (
    delete_7year_deleted_documents as _cleanup_7year,
)
from .services.cleanup import (
    delete_orphaned_documents as _cleanup_orphaned,
)
from .services.cleanup import normalize_s3_key
from .services.cleanup import (
    reconcile_documents as _cleanup_reconcile,
)
from .services.ocr import MAX_OCR_RETRIES
from .services.ocr import GeminiOCRError as GeminiOCRError
from .services.ocr import extract as _ocr_extract
from .storage import BUCKET, get_s3_client

__all__ = [
    "GeminiOCRError",
]


@shared_task(retries=MAX_OCR_RETRIES, backoff_factor=2)
def extract_document(document_id: int) -> dict[str, Any]:
    """Run Gemini OCR on a document, extracting structured financial data."""
    return _ocr_extract(document_id)


@shared_task(retries=3, backoff_factor=2)
def delete_document(filepath: str) -> None:
    """Delete a single file from R2 storage, retrying on transient failures."""
    if filepath:
        s3 = get_s3_client()
        s3.delete_object(Bucket=BUCKET, Key=normalize_s3_key(filepath))


@shared_task
def delete_orphaned_documents() -> None:
    """Remove unlinked documents after a grace period."""
    _cleanup_orphaned()


@shared_task
def reconcile_documents() -> None:
    """Clean up stale pending uploads and dangling error records."""
    _cleanup_reconcile()


@shared_task
def delete_7year_deleted_documents() -> None:
    """Hard-delete documents soft-deleted over 7 years ago for users with auto-delete enabled."""
    _cleanup_7year()
