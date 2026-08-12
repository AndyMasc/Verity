"""Background tasks for OCR extraction, document deletion, and storage reconciliation.

Uses Dramatiq for async execution with retry/backoff. Each task is a thin
wrapper that delegates to the corresponding service module.
"""

from typing import Any

import dramatiq
from django.conf import settings
from dramatiq.rate_limits import BucketRateLimiter
from dramatiq.rate_limits.backends import RedisBackend
from periodiq import cron

from .services.cleanup import (
    delete_orphaned_documents as _cleanup_orphaned,
)
from .services.cleanup import normalize_s3_key
from .services.cleanup import (
    reconcile_documents as _cleanup_reconcile,
)
from .services.ocr import MAX_OCR_RETRIES
from .services.ocr import extract as _ocr_extract
from .storage import BUCKET, get_s3_client

REDIS_URL = settings.REDIS_URL
backend = RedisBackend(url=REDIS_URL)
rate_limiter = BucketRateLimiter(backend, "ocr-rpm-limiter", limit=1, bucket=4000) # 1 request per 4 seconds (15 requests per minute) to avoid boundary bursts (eg, 15 requests in the last second of a minute).


@dramatiq.actor(queue_name="ocr-tasks", max_retries=MAX_OCR_RETRIES, min_backoff=10_000, max_backoff=300_000)
def extract_document(document_id: int) -> dict[str, Any]:
    """Run Gemini OCR on a document and auto-create a Record from the result.2

    The record is created from the persisted "ocr_raw_data" so it survives
    even if the user closes the tab before the redirect. Merging with a Plaid
    match (when warranted) happens inside "create_record_from_ocr".
    The task is set to retry on transient failures, and is rate-limited to avoid overloading the OCR service.
    """
    with rate_limiter.acquire():
        result = _ocr_extract(document_id)
    if isinstance(result, dict) and "error" not in result:
        from records.services import create_record_from_ocr
        create_record_from_ocr(document_id)
    return result


@dramatiq.actor(queue_name="maintenance", max_retries=3, min_backoff=2000)
def delete_document(filepath: str) -> None:
    """Delete a single file from R2 storage, retrying on transient failures."""
    if filepath:
        s3 = get_s3_client()
        s3.delete_object(Bucket=BUCKET, Key=normalize_s3_key(filepath))


@dramatiq.actor(queue_name="maintenance", periodic=cron("0 4 * * *"))
def delete_orphaned_documents() -> None:
    """Remove unlinked documents after a grace period."""
    _cleanup_orphaned()


@dramatiq.actor(periodic=cron("0 * * * *"))
def reconcile_documents() -> None:
    """Clean up stale pending uploads and dangling error records."""
    _cleanup_reconcile()
