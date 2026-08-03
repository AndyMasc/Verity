"""URL routing for the documents module.

Maps endpoints for upload, confirmation, viewing, deletion, trash management,
and OCR status listing. All routes require authentication.
"""

from django.urls import path

from . import views

app_name = "documents"
urlpatterns = [
    path("upload/", views.UploadView.as_view(), name="upload_document"),
    path("confirm/", views.ConfirmUploadView.as_view(), name="confirm_upload"),
    path("view/<int:pk>/", views.ViewDocument.as_view(), name="view_document"),
    path(
        "delete/<int:document_id>/",
        views.DeleteDocument.as_view(),
        name="delete_document",
    ),
    path(
        "add-supporting/<int:record_id>/",
        views.AddSupportDocuments.as_view(),
        name="add_support_docs",
    ),
    path("document_lists/", views.DocumentListView.as_view(), name="document_list_view"),
    path("pending-ocr/", views.PendingOCRListView.as_view(), name="pending_ocr"),
    path("trash/", views.TrashDocumentListView.as_view(), name="trash_list"),
    path(
        "trash/<int:pk>/undo/",
        views.UndoDeleteDocument.as_view(),
        name="undo_delete_document",
    ),
    path(
        "hard-delete/<int:pk>/",
        views.HardDeleteDocumentView.as_view(),
        name="hard_delete_document",
    ),
]
