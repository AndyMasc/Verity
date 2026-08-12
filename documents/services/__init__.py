"""Service layer for all document operations.

Re-exports key classes and functions for convenient access from other modules.

The OCR pipeline (``.ocr``) is intentionally NOT re-exported here: it pulls in
the Gemini client and image-processing stack (OpenCV/numpy), which only
background workers need. Import it directly as ``documents.services.ocr``.
"""

from .cleanup import (
    bulk_delete_documents,
    delete_orphaned_documents,
    reconcile_documents,
)
from .deletion import DocumentDeletionService
from .detail import DocumentDetailService
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
    "reconcile_documents",
]
