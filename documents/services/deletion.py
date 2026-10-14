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
    def delete(document: DocumentData) -> DeletionResult:
        """Delete a document from the database and queue R2 cleanup."""
        record_id = document.associated_record_id
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
