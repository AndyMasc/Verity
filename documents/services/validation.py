"""Upload validation and confirmation service for R2 document uploads.

Validates that the R2 object exists and passes gatekeeper checks, then
optionally transitions the document from PENDING_UPLOAD to UPLOADED status.
"""

import logging
from dataclasses import dataclass

from documents.models import DocumentData, DocumentStatus
from documents.storage import (
    gatekeeper_validate_r2_object,
    get_r2_object_head,
    verify_r2_object_exists,
)

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
    - ``validate()``: runs all checks and returns metadata on success.
    - ``confirm()``: runs all checks and transitions document to UPLOADED on success.
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
        if self.document.filepath != self.key:
            logger.warning(
                "Key mismatch for doc %s: expected=%s, received=%s",
                self.document.id,
                self.document.filepath,
                self.key,
            )
            return UploadResult(valid=False, error="Key mismatch.", status_code=400)

        if not verify_r2_object_exists(self.key):
            self.document.status = DocumentStatus.ERROR
            self.document.save(update_fields=["status"])
            return UploadResult(valid=False, error="File not found in storage.", status_code=404)

        validation = gatekeeper_validate_r2_object(self.key)
        if not validation["valid"]:
            self.document.status = DocumentStatus.ERROR
            self.document.notes = (
                (self.document.notes or "") + f"\n[Gatekeeper] {validation['error']}"
            ).strip()
            self.document.save(update_fields=["status", "notes"])
            logger.warning("Gatekeeper rejected doc %s: %s", self.document.id, validation["error"])
            return UploadResult(valid=False, error=validation["error"], status_code=422)

        head = get_r2_object_head(self.key)
        file_size = head.get("ContentLength") if head else None
        mime_type = head.get("ContentType", "").split(";")[0].strip() if head else ""

        if transition:
            self.document.status = DocumentStatus.UPLOADED
            self.document.file_size = file_size
            self.document.mime_type = mime_type or self.document.mime_type
            self.document.save(update_fields=["status", "file_size", "mime_type"])

        return UploadResult(
            valid=True,
            document=self.document if transition else None,
            file_size=file_size,
            mime_type=mime_type,
        )
