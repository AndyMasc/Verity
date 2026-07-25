"""Document deletion service for soft-delete, hard-delete, and undo operations.

Encapsulates the business logic around document lifecycle transitions:
soft-deleting with OCR-aware messaging, undoing soft-deletes, permanently
deleting aged documents with R2 cleanup, and determining redirect targets.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from documents.models import DocumentData
from documents.services.cleanup import COMPLIANCE_RETENTION_YEARS

logger = logging.getLogger(__name__)


@dataclass
class DeletionResult:
    """Outcome of a document deletion operation."""

    success: bool = True
    error: str | None = None
    message: str = ""
    message_tag: str = "success"
    record_id: int | None = None
    filepath: str | None = None


class DocumentDeletionService:
    """Handles document deletion business logic across all deletion modes."""

    @staticmethod
    def soft_delete(document: DocumentData) -> DeletionResult:
        """Soft or hard-delete a document depending on OCR status.

        OCR'd documents are soft-deleted for compliance retention.
        Non-OCR documents are hard-deleted immediately.
        """
        record_id = document.associated_record_id if document.associated_record else None
        filepath = document.filepath

        try:
            document.delete()
            if document.did_ocr:
                message = (
                    "Document removed from record. Critical documents are preserved for compliance."
                )
                message_tag = "info"
            else:
                message = "Document deleted permanently."
                message_tag = "success"
        except Exception as e:
            logger.error(
                "Failed to delete document %s: %s",
                document.pk,
                e,
                exc_info=True,
            )
            return DeletionResult(
                success=False,
                error="Failed to complete deletion safely due to a system error.",
                record_id=record_id,
            )

        return DeletionResult(
            success=True,
            message=message,
            message_tag=message_tag,
            record_id=record_id,
            filepath=filepath,
        )

    @staticmethod
    def undo_delete(document: DocumentData) -> DeletionResult:
        """Restore a soft-deleted document to active status."""
        document.undo_delete()
        return DeletionResult(success=True, message="Document restored.")

    @staticmethod
    def is_eligible_for_hard_delete(document: DocumentData) -> bool:
        """Check whether a document has exceeded the compliance retention period."""
        seven_years_ago = timezone.now() - timedelta(days=365 * COMPLIANCE_RETENTION_YEARS)
        return document.date_added <= seven_years_ago

    @staticmethod
    def hard_delete(document: DocumentData) -> DeletionResult:
        """Permanently delete a document from the database and queue R2 cleanup.

        Returns the filepath so the caller can trigger async R2 deletion.
        """
        filepath = document.filepath
        document.hard_delete()
        return DeletionResult(
            success=True,
            message="Document permanently deleted.",
            filepath=filepath,
        )
