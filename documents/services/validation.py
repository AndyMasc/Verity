"""Upload validation and confirmation service for R2 document uploads.

Validates that the R2 object exists and passes gatekeeper checks, then
optionally transitions the document from PENDING_UPLOAD to UPLOADED status.
"""

import logging
from dataclasses import dataclass

from django.db import transaction

from billing.entitlements import record_monthly_use
from documents.models import DocumentData, DocumentStatus
from documents.storage import get_r2_object_head
from documents.validators import MAX_FILE_SIZE

logger = logging.getLogger(__name__)


@dataclass
class UploadResult:
    """Outcome of an upload validation or confirmation attempt."""

    valid: bool = True
    error: str | None = None
    status_code: int = 200
    document: DocumentData | None = None
    file_size: int | None = None
    mime_type: str | None = None


class DocumentUploadService:
    """Validates and confirms R2 uploads for documents.

    Provides two entry points:
    - validate(): runs all checks and returns metadata on success.
    - confirm(): runs all checks and transitions document to UPLOADED on success.
    """

    def __init__(self, document: DocumentData, key: str):
        self.document = document
        self.key = key

    def validate(self) -> UploadResult:
        """Run all validation checks and return file metadata on success.

        Checks document status, key consistency, R2 object existence, and
        gatekeeper rules. Updates document status to ERROR on storage or
        validation failures. Does NOT transition to UPLOADED.
        """
        if self.document.status != DocumentStatus.PENDING_UPLOAD:
            return UploadResult(
                valid=False,
                error=f"Unexpected status: {self.document.status}.",
                status_code=409,
            )

        return self._run_checks(transition=False)

    def confirm(self) -> UploadResult:
        """Run all validation checks and transition to UPLOADED on success.

        Checks key consistency, R2 object existence, and gatekeeper rules.
        Returns an UploadResult indicating success or the specific failure.
        """
        return self._run_checks(transition=True)

    def _run_checks(self, *, transition: bool) -> UploadResult:
        """Execute the shared validation pipeline.

        When *transition* is True, a successful validation also sets the
        document status to UPLOADED.
        """

        def reject(note: str, error: str, status_code: int) -> UploadResult:
            """Mark the document ERROR, record why, and return the failure."""
            self.document.status = DocumentStatus.ERROR
            self.document.notes = ((self.document.notes or "") + f"\n{note}").strip()
            self.document.save(update_fields=["status", "notes"])
            logger.warning("Upload rejected for doc %s: %s", self.document.id, note)
            return UploadResult(valid=False, error=error, status_code=status_code)

        if self.document.filepath != self.key:
            logger.warning(
                "Key mismatch for doc %s: expected=%s, received=%s",
                self.document.id,
                self.document.filepath,
                self.key,
            )
            self.document.status = DocumentStatus.ERROR
            self.document.save(update_fields=["status"])
            return UploadResult(valid=False, error="Key mismatch.", status_code=400)

        head = get_r2_object_head(self.key)
        if head is None:
            self.document.status = DocumentStatus.ERROR
            self.document.save(update_fields=["status"])
            return UploadResult(valid=False, error="File not found in storage.", status_code=404)

        file_size = head.get("ContentLength")
        mime_type = (head.get("ContentType") or "").split(";")[0].strip()

        if file_size == 0:
            return reject(
                "[Gatekeeper] Empty file rejected.",
                "Empty file rejected.",
                422,
            )

        if file_size is not None and file_size > MAX_FILE_SIZE:
            limit_mb = MAX_FILE_SIZE / 1024 / 1024
            return reject(
                f"[Gatekeeper] File exceeds {limit_mb}MB limit.",
                f"File exceeds {limit_mb}MB limit.",
                422,
            )

        if transition:
            # Confirmations can be retried or arrive concurrently. Only the
            # conditional pending -> uploaded update that wins may record usage.
            with transaction.atomic():
                new_mime_type = mime_type or self.document.mime_type
                transitioned = DocumentData.objects.filter(
                    pk=self.document.pk,
                    status=DocumentStatus.PENDING_UPLOAD,
                ).update(
                    status=DocumentStatus.UPLOADED,
                    file_size=file_size,
                    mime_type=new_mime_type,
                )
                if transitioned:
                    self.document.status = DocumentStatus.UPLOADED
                    self.document.file_size = file_size
                    self.document.mime_type = new_mime_type
                    record_monthly_use(self.document.user, "upload")

        return UploadResult(
            valid=True,
            document=self.document if transition else None,
            file_size=file_size,
            mime_type=mime_type,
        )
