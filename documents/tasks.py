"""Background tasks for OCR extraction, document deletion, and storage reconciliation.

Uses Dramatiq for async execution with retry/backoff. Each task is a thin
wrapper that delegates to the corresponding service module.
"""

from typing import Any

import dramatiq

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


@dramatiq.actor(max_retries=MAX_OCR_RETRIES, min_backoff=2)
def extract_document(document_id: int) -> dict[str, Any]:
    """Run Gemini OCR on a document and auto-create a Record from the result.

    The record is created from the persisted ``ocr_raw_data`` so it survives
    even if the user closes the tab before the redirect. Merging with a Plaid
    match (when warranted) happens inside ``create_record_from_ocr``.
    """
    result = _ocr_extract(document_id)
    if isinstance(result, dict) and "error" not in result:
        from records.services import create_record_from_ocr

        create_record_from_ocr(document_id)
    return result


@dramatiq.actor(max_retries=3, min_backoff=2)
def delete_document(filepath: str) -> None:
    """Delete a single file from R2 storage, retrying on transient failures."""
    if filepath:
        s3 = get_s3_client()
        s3.delete_object(Bucket=BUCKET, Key=normalize_s3_key(filepath))


@dramatiq.actor
def delete_orphaned_documents() -> None:
    """Remove unlinked documents after a grace period."""
    _cleanup_orphaned()


@dramatiq.actor
def reconcile_documents() -> None:
    """Clean up stale pending uploads and dangling error records."""
    _cleanup_reconcile()
