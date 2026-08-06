"""Tests for the document cleanup service.

Covers normalize_s3_key, bulk_delete_documents, delete_orphaned_documents,
and reconcile_documents.
"""

import hashlib
from datetime import timedelta
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from documents.services.cleanup import (
    bulk_delete_documents,
    delete_orphaned_documents,
    normalize_s3_key,
    reconcile_documents,
)

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestNormalizeS3Key:
    def test_strips_leading_slash(self):
        assert normalize_s3_key("/users/1/file.pdf") == "users/1/file.pdf"

    def test_strips_multiple_leading_slashes(self):
        assert normalize_s3_key("///users/1/file.pdf") == "users/1/file.pdf"

    def test_returns_empty_string_for_empty(self):
        assert normalize_s3_key("") == ""

    def test_returns_empty_string_for_none(self):
        assert normalize_s3_key(None) == ""  # type: ignore[arg-type]

    def test_no_slash_prefix_unchanged(self):
        assert normalize_s3_key("users/1/file.pdf") == "users/1/file.pdf"

    def test_preserves_internal_slashes(self):
        assert normalize_s3_key("/a/b/c.pdf") == "a/b/c.pdf"


@pytest.mark.django_db
class TestBulkDeleteDocuments:
    def test_deletes_db_records(self, user):
        docs = [
            DocumentData.objects.create(
                user=user,
                filepath=f"users/{user.id}/doc{i}.pdf",
                file_hash=_make_hash(f"doc{i}".encode()),
                status=DocumentStatus.COMPLETED,
                did_ocr=True,
            )
            for i in range(3)
        ]
        file_data = [(d.id, d.filepath) for d in docs]
        bulk_delete_documents(file_data)
        for d in docs:
            assert not DocumentData.objects.filter(id=d.id).exists()

    @patch("documents.services.cleanup.get_s3_client")
    def test_calls_r2_batch_delete(self, mock_get_s3, user):
        docs = [
            DocumentData.objects.create(
                user=user,
                filepath=f"users/{user.id}/doc{i}.pdf",
                file_hash=_make_hash(f"doc{i}".encode()),
            )
            for i in range(2)
        ]
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        file_data = [(d.id, d.filepath) for d in docs]
        bulk_delete_documents(file_data)
        mock_s3.delete_objects.assert_called_once()

    @patch("documents.services.cleanup.get_s3_client")
    def test_skips_r2_when_no_filepaths(self, mock_get_s3, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="",
            file_hash=_make_hash(b"empty"),
        )
        file_data = [(doc.id, "")]
        bulk_delete_documents(file_data)
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        mock_s3.delete_objects.assert_not_called()

    @patch("documents.services.cleanup.get_s3_client")
    def test_r2_failure_does_not_prevent_db_delete(self, mock_get_s3, user):
        mock_s3 = MagicMock()
        mock_s3.delete_objects.side_effect = Exception("R2 down")
        mock_get_s3.return_value = mock_s3
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/fail.pdf",
            file_hash=_make_hash(b"fail"),
        )
        file_data = [(doc.id, doc.filepath)]
        bulk_delete_documents(file_data)
        assert not DocumentData.objects.filter(id=doc.id).exists()

    def test_empty_list_is_noop(self, user):
        bulk_delete_documents([])

    @patch("documents.services.cleanup.get_s3_client")
    def test_chunking_with_many_records(self, mock_get_s3, user):
        docs = [
            DocumentData.objects.create(
                user=user,
                filepath=f"users/{user.id}/chunk{i}.pdf",
                file_hash=_make_hash(f"chunk{i}".encode()),
            )
            for i in range(5)
        ]
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        file_data = [(d.id, d.filepath) for d in docs]
        bulk_delete_documents(file_data)
        for d in docs:
            assert not DocumentData.objects.filter(id=d.id).exists()


@pytest.mark.django_db
class TestDeleteOrphanedDocuments:
    def test_deletes_non_ocr_orphans_after_1_day(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/old.pdf",
            file_hash=_make_hash(b"old"),
            did_ocr=False,
        )
        DocumentData.objects.filter(id=doc.id).update(date_added=timezone.now() - timedelta(days=2))
        with patch("documents.services.cleanup.bulk_delete_documents") as mock_bulk:
            delete_orphaned_documents()
            mock_bulk.assert_called_once()

    def test_keeps_recent_non_ocr_orphans(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/new.pdf",
            file_hash=_make_hash(b"new"),
            did_ocr=False,
        )
        with patch("documents.services.cleanup.bulk_delete_documents") as mock_bulk:
            delete_orphaned_documents()
            mock_bulk.assert_not_called()

    def test_deletes_ocr_orphans_after_7_days(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/ocr_old.pdf",
            file_hash=_make_hash(b"ocr_old"),
            did_ocr=True,
            status=DocumentStatus.UPLOADED,
        )
        DocumentData.objects.filter(id=doc.id).update(date_added=timezone.now() - timedelta(days=8))
        with patch("documents.services.cleanup.bulk_delete_documents") as mock_bulk:
            delete_orphaned_documents()
            mock_bulk.assert_called_once()

    def test_keeps_linked_documents(self, user):
        from records.models import Record

        record = Record.objects.create(
            user=user,
            title="Linked",
            record_type="expense_receipt",
            transaction_date=timezone.now().date(),
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/linked.pdf",
            file_hash=_make_hash(b"linked"),
            associated_record=record,
        )
        DocumentData.objects.filter(id=doc.id).update(
            date_added=timezone.now() - timedelta(days=10)
        )
        with patch("documents.services.cleanup.bulk_delete_documents") as mock_bulk:
            delete_orphaned_documents()
            mock_bulk.assert_not_called()

    def test_excludes_deleting_status_docs(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/deleting.pdf",
            file_hash=_make_hash(b"deleting"),
            did_ocr=False,
            status=DocumentStatus.DELETING,
        )
        DocumentData.objects.filter(id=doc.id).update(date_added=timezone.now() - timedelta(days=2))
        with patch("documents.services.cleanup.bulk_delete_documents") as mock_bulk:
            delete_orphaned_documents()
            mock_bulk.assert_not_called()


@pytest.mark.django_db
class TestReconcileDocuments:
    @patch("documents.services.cleanup.get_s3_client")
    def test_deletes_stale_pending_uploads(self, mock_get_s3, user):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/stale.pdf",
            file_hash=_make_hash(b"stale"),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        DocumentData.objects.filter(id=doc.id).update(
            date_added=timezone.now() - timedelta(minutes=45)
        )
        reconcile_documents()
        assert not DocumentData.objects.filter(id=doc.id).exists()

    @patch("documents.services.cleanup.get_s3_client")
    def test_keeps_recent_pending_uploads(self, mock_get_s3, user):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/recent.pdf",
            file_hash=_make_hash(b"recent"),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        reconcile_documents()
        assert DocumentData.objects.filter(id=doc.id).exists()

    @patch("documents.services.cleanup.delete_r2_objects_batch")
    @patch("documents.services.cleanup.get_s3_client")
    def test_removes_dangling_error_records(self, mock_get_s3, mock_r2, user):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        doc = DocumentData.objects.create(
            user=user,
            filepath=f"users/{user.id}/dangling.pdf",
            file_hash=_make_hash(b"dangling"),
            status=DocumentStatus.ERROR,
        )
        DocumentData.objects.filter(id=doc.id).update(date_added=timezone.now() - timedelta(days=3))
        reconcile_documents()
        assert not DocumentData.objects.filter(id=doc.id).exists()
        mock_r2.assert_called_once()
