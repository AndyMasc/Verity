"""Public view classes exposed by the documents views package.

Re-exports every view from the sub-modules so they can be imported
directly as ``documents.views.UploadView`` etc.
"""

from .detail import DeleteDocument, ViewDocument
from .list import DocumentListView
from .upload import AddSupportDocuments, ConfirmUploadView, UploadView

__all__ = [
    "AddSupportDocuments",
    "ConfirmUploadView",
    "DeleteDocument",
    "DocumentListView",
    "UploadView",
    "ViewDocument",
]
