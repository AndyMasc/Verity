"""URL configuration for the records app.

Routes cover record CRUD, folder management, merge operations (manual,
undo, detach, replace), archive/unarchive, hard-delete, and the merge
list view. All paths live under the "records:" namespace.
"""

from django.urls import path

from . import views
from .views import shares as share_views

app_name = "records"
urlpatterns = [
    path(
        "record_detail/<int:pk>/history/",
        views.RecordHistoryView.as_view(),
        name="record_history",
    ),
    path(
        "record_detail/<int:pk>/shares/",
        share_views.RecordSharingSectionView.as_view(),
        name="record_shares_panel",
    ),
    path(
        "record_detail/<int:pk>/shares/add/",
        share_views.ShareRecordView.as_view(),
        name="record_share",
    ),
    path(
        "record_detail/<int:pk>/shares/<int:share_pk>/revoke/",
        share_views.RevokeShareView.as_view(),
        name="record_share_revoke",
    ),
    path("view_all_records/", views.RecordListView.as_view(), name="view_all_records"),
    path(
        "add_record/<int:document_id>/",
        views.AddRecordView.as_view(),
        name="add_record",
    ),
    path("add_record/", views.AddRecordView.as_view(), name="add_record_manual"),
    path(
        "record_detail/<int:pk>/",
        views.RecordDetailView.as_view(),
        name="record_detail",
    ),
    path(
        "archive_record/<int:record_id>/",
        views.ArchiveRecord.as_view(),
        name="archive_record",
    ),
    path(
        "unarchive/<int:record_id>/",
        views.UnarchiveRecord.as_view(),
        name="unarchive_record",
    ),
    path(
        "delete_record/<int:record_id>/",
        views.DeleteRecordView.as_view(),
        name="delete_record",
    ),
    path(
        "check_ocr_status/<int:document_id>/",
        views.CheckOCRStatus.as_view(),
        name="check_ocr_status",
    ),
    path("folders/", views.FolderListView.as_view(), name="view_folders"),
    path("create_folder/", views.CreateFolder.as_view(), name="create_folder"),
    path(
        "folders/<int:folder_id>/edit/",
        views.FolderUpdateView.as_view(),
        name="edit_folder",
    ),
    path(
        "folders/<int:folder_id>/delete/",
        views.FolderDeleteView.as_view(),
        name="delete_folder",
    ),
    path("merges/manual/", views.ManualMergeView.as_view(), name="manual_merge"),
    path(
        "merges/manual/search/<str:mode>/",
        views.ManualMergeSearchView.as_view(),
        name="manual_merge_search",
    ),
    path(
        "merges/manual/modal/<str:mode>/",
        views.ManualMergeModalView.as_view(),
        name="manual_merge_modal",
    ),
    path("merges/<int:merge_id>/undo/", views.UndoMergeView.as_view(), name="undo_merge"),
    path(
        "hard-delete/<int:pk>/",
        views.HardDeleteRecordView.as_view(),
        name="hard_delete_record",
    ),
    path(
        "bulk-archive/",
        views.BulkArchiveView,
        name="bulk_archive",
    ),
    path(
        "bulk-unarchive/",
        views.BulkUnarchiveView,
        name="bulk_unarchive",
    ),
    path(
        "bulk-share/",
        share_views.BulkShareView.as_view(),
        name="bulk_share",
    ),
]
