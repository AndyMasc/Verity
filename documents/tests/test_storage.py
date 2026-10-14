import hashlib
import io
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from documents.models import DocumentData, DocumentStatus
from documents.storage import (
    generate_upload_key,
    generate_presigned_post,
    gatekeeper_validate_r2_object,
    generate_read_presigned_url,
    validate_uploaded_bytes,
    verify_r2_object_exists,
)
from documents.services.cleanup import bulk_delete_documents as _bulk_delete_documents


class BulkDeleteDocumentsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="cleanupuser", password="pass")
        self.docs = []
        for i in range(3):
            doc = DocumentData.objects.create(
                user=self.user,
                filepath=f"users/1/doc_{i}.pdf",
                file_hash=hashlib.sha256(f"doc_{i}".encode()).hexdigest(),
                status=DocumentStatus.COMPLETED,
                did_ocr=True,
            )
            self.docs.append(doc)

    @patch("documents.services.cleanup.get_s3_client")
    def test_deletes_db_records(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        file_data = [(d.id, d.filepath) for d in self.docs]
        _bulk_delete_documents(file_data)
        for doc in self.docs:
            self.assertFalse(DocumentData.objects.filter(id=doc.id).exists())

    @patch("documents.services.cleanup.get_s3_client")
    def test_deletes_r2_objects(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        file_data = [(d.id, d.filepath) for d in self.docs]
        _bulk_delete_documents(file_data)
        expected_keys = [f"users/1/doc_{i}.pdf" for i in range(3)]
        mock_s3.delete_objects.assert_called_once()
        actual_keys = [o["Key"] for o in mock_s3.delete_objects.call_args[1]["Delete"]["Objects"]]
        self.assertCountEqual(actual_keys, expected_keys)

    @patch("documents.services.cleanup.get_s3_client")
    def test_deletes_db_first_then_r2(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        call_order = []

        with patch.object(DocumentData.objects, "filter") as mock_filter:
            mock_qs = mock_filter.return_value
            mock_qs.delete.side_effect = lambda: call_order.append("db")

            def mock_r2(*args, **kwargs):
                call_order.append("r2")

            mock_s3.delete_objects.side_effect = mock_r2

            file_data = [(self.docs[0].id, self.docs[0].filepath)]
            _bulk_delete_documents(file_data)

        self.assertEqual(call_order, ["db", "r2"])

    @patch("documents.services.cleanup.get_s3_client")
    def test_db_failure_skips_r2(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        file_data = [(self.docs[0].id, self.docs[0].filepath)]

        with patch.object(DocumentData.objects, "filter") as mock_filter:
            mock_qs = mock_filter.return_value
            mock_qs.delete.side_effect = Exception("DB error")

            _bulk_delete_documents(file_data)

        mock_s3.delete_objects.assert_not_called()

    @patch("documents.services.cleanup.get_s3_client")
    def test_r2_failure_does_not_rollback_db(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.delete_objects.side_effect = Exception("R2 error")
        mock_get_s3.return_value = mock_s3

        doc = self.docs[0]
        file_data = [(doc.id, doc.filepath)]
        _bulk_delete_documents(file_data)

        self.assertFalse(DocumentData.objects.filter(id=doc.id).exists())

    @patch("documents.services.cleanup.get_s3_client")
    def test_no_filepath_skips_r2(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="",
            file_hash=hashlib.sha256(b"nopath").hexdigest(),
            status=DocumentStatus.COMPLETED,
            did_ocr=True,
        )
        file_data = [(doc.id, doc.filepath)]
        _bulk_delete_documents(file_data)
        mock_s3.delete_objects.assert_not_called()
        self.assertFalse(DocumentData.objects.filter(id=doc.id).exists())
