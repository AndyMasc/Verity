"""Service layer for all document operations.

Re-exports key classes and functions for convenient access from other modules.
"""

from .cleanup import (
    bulk_delete_documents,
    delete_orphaned_documents,
    reconcile_documents,
)
from .deletion import DocumentDeletionService
from .detail import DocumentDetailService
from .ocr import extract
from .upload import UploadService
from .validation import DocumentUploadService, UploadResult

ConfirmUploadService = DocumentUploadService

__all__ = [
    "ConfirmUploadService",
    "DocumentDeletionService",
    "DocumentDetailService",
    "DocumentUploadService",
    "UploadResult",
    "UploadService",
    "bulk_delete_documents",
    "delete_orphaned_documents",
    "extract",
    "reconcile_documents",
]
