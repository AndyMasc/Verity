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


class StorageUtilsTest(TestCase):
    def test_generate_upload_key(self):
        key = generate_upload_key(1, "pdf")
        self.assertTrue(key.startswith("users/1/"))
        self.assertTrue(key.endswith(".pdf"))
        self.assertNotEqual(key, "users/1/.pdf")

    def test_generate_upload_key_no_extension(self):
        key = generate_upload_key(1, "")
        self.assertTrue(key.startswith("users/1/"))
        self.assertIn(".", key)

    @patch("documents.storage.get_s3_client")
    def test_generate_presigned_post(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://example.com/presigned-url"
        mock_get_s3.return_value = mock_s3
        result = generate_presigned_post(1, "users/1/test.pdf", "application/pdf")
        self.assertEqual(result, "https://example.com/presigned-url")

    @patch("documents.storage.get_s3_client")
    def test_generate_read_presigned_url(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = "https://example.com/read-url"
        mock_get_s3.return_value = mock_s3
        url = generate_read_presigned_url("users/1/test.pdf")
        self.assertEqual(url, "https://example.com/read-url")

    @patch("documents.storage.get_s3_client")
    def test_verify_r2_object_exists(self, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}
        mock_get_s3.return_value = mock_s3
        result = verify_r2_object_exists("users/1/test.pdf")
        self.assertTrue(result)

    @patch("documents.storage.get_s3_client")
    def test_verify_r2_object_not_found(self, mock_get_s3):
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}},
            "HeadObject",
        )
        mock_get_s3.return_value = mock_s3
        result = verify_r2_object_exists("users/1/missing.pdf")
        self.assertFalse(result)

    @patch("documents.storage.get_s3_client")
    @patch("documents.storage.get_r2_object_head")
    def test_gatekeeper_validate_valid(self, mock_head, mock_get_s3):
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": io.BytesIO(b"%PDF-1.4 test content")}
        mock_get_s3.return_value = mock_s3
        mock_head.return_value = {"ContentLength": 100}
        result = gatekeeper_validate_r2_object("users/1/test.pdf")
        self.assertTrue(result["valid"])
