"""Upload confirmation service — re-exports from validation module.

The confirmation logic is now part of the unified ``DocumentUploadService``
in ``documents.services.validation``. This module re-exports for backward
compatibility.
"""

from documents.services.validation import (  # noqa: F401
    DocumentUploadService as ConfirmUploadService,
)
