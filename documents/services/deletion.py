"""Document deletion service for permanent document removal.

Encapsulates the business logic around document deletion: every delete is
permanent (database row removed immediately, R2 file cleaned up via signals).
There is no trash, soft-delete, or undo path for documents.
"""

import logging
from dataclasses import dataclass

from documents.models import DocumentData

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
    """Handles document deletion business logic."""

    @staticmethod
    def soft_delete(document: DocumentData) -> DeletionResult:
        """Permanently delete a document from the database and queue R2 cleanup.

        Retained for API stability; every document delete is now permanent.
        """
        record_id = document.associated_record_id if document.associated_record else None
        filepath = document.filepath

        try:
            document.delete()
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
            message="Document deleted permanently.",
            message_tag="success",
            record_id=record_id,
            filepath=filepath,
        )

    @staticmethod
    def hard_delete(document: DocumentData) -> DeletionResult:
        """Permanently delete a document from the database and queue R2 cleanup.

        Identical to soft_delete; kept as a thin alias for callers that
        explicitly want a permanent delete.
        """
        return DocumentDeletionService.soft_delete(document)
