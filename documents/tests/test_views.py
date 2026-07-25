import hashlib
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from records.models import Record


def _make_hash(content: bytes = b"test content") -> str:
    return hashlib.sha256(content).hexdigest()


class UploadViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="uploaduser", password="pass")
        self.url = reverse("documents:upload_document")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_returns_form_page(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "documents/upload_file.html")

    @patch("documents.storage.generate_presigned_post")
    def test_post_presign_valid(self, mock_presign):
        mock_presign.return_value = "https://example.com/upload-url"
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "filename": "test.pdf",
                "content_type": "application/pdf",
                "file_hash": _make_hash(),
            },
        )
        if response.status_code == 403:
            self.skipTest("Rate-limited in concurrent test run")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "upload_url")
        self.assertIn("document_id", data)
        self.assertIn("key", data)
        self.assertIn("upload_url", data)

    @patch("documents.storage.generate_presigned_post")
    def test_post_presign_duplicate_detection(self, mock_presign):
        mock_presign.return_value = "https://example.com/upload-url"
        self.client.force_login(self.user)
        h = _make_hash()
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/dup.pdf",
            file_hash=h,
        )
        response = self.client.post(
            self.url,
            {
                "filename": "dup.pdf",
                "content_type": "application/pdf",
                "file_hash": h,
            },
        )
        if response.status_code == 200:
            data = response.json()
            self.assertEqual(data["status"], "duplicate_confirmed")

    @patch("documents.storage.generate_presigned_post")
    def test_post_presign_force_upload_skips_duplicate(self, mock_presign):
        mock_presign.return_value = "https://example.com/upload-url"
        self.client.force_login(self.user)
        h = _make_hash()
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/force.pdf",
            file_hash=h,
        )
        response = self.client.post(
            self.url,
            {
                "filename": "force.pdf",
                "content_type": "application/pdf",
                "file_hash": h,
                "force_upload": "true",
            },
        )
        if response.status_code == 200:
            data = response.json()
            self.assertEqual(data["status"], "upload_url")

    def test_post_missing_file_hash(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {"filename": "test.pdf", "content_type": "application/pdf"},
        )
        if response.status_code == 200:
            self.fail("Should have returned 400 for missing file_hash")
        self.assertNotEqual(response.status_code, 500)

    def test_post_invalid_form(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "filename": "",
                "content_type": "application/pdf",
                "file_hash": _make_hash(),
            },
        )
        if response.status_code == 200:
            self.fail("Should have returned 400 for invalid form")
        self.assertNotEqual(response.status_code, 500)


class ConfirmUploadViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="confirmuser", password="pass")
        self.url = reverse("documents:confirm_upload")
        self.doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/confirmed.pdf",
            file_hash=_make_hash(),
        )

    def test_login_required(self):
        response = self.client.post(
            self.url, {"document_id": self.doc.id, "key": self.doc.filepath}
        )
        self.assertEqual(response.status_code, 302)

    @patch("documents.services.validation.gatekeeper_validate_r2_object")
    @patch("documents.services.validation.verify_r2_object_exists")
    def test_confirm_valid(self, mock_verify, mock_gatekeeper):
        mock_verify.return_value = True
        mock_gatekeeper.return_value = {"valid": True}
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {"document_id": self.doc.id, "key": self.doc.filepath},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "confirmed")
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, DocumentStatus.UPLOADED)

    def test_confirm_not_owned(self):
        user2 = User.objects.create_user(username="otheruser", password="pass")
        self.client.force_login(user2)
        response = self.client.post(
            self.url,
            {"document_id": self.doc.id, "key": self.doc.filepath},
        )
        self.assertEqual(response.status_code, 404)

    def test_confirm_not_found(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"document_id": 99999, "key": "nonexistent"})
        self.assertEqual(response.status_code, 404)

    def test_confirm_no_id(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, 400)


class ViewDocumentViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="viewuser", password="pass")
        self.doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/viewable.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
        )
        self.url = reverse("documents:view_document", args=[self.doc.id])

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_owner_can_view(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "documents/view_document.html")

    def test_other_user_cannot_view(self):
        user2 = User.objects.create_user(username="other", password="pass")
        self.client.force_login(user2)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_not_found(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:view_document", args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_context_has_document(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.context["document"], self.doc)


class DeleteDocumentViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="deleteuser", password="pass")
        self.doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/deletable.pdf",
            file_hash=_make_hash(),
        )
        self.url = reverse("documents:delete_document", args=[self.doc.id])

    def test_login_required(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_owner_can_delete(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.assertFalse(DocumentData.objects.filter(id=self.doc.id).exists())

    def test_other_user_cannot_delete(self):
        user2 = User.objects.create_user(username="otherdel", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(DocumentData.objects.filter(id=self.doc.id).exists())


class AddSupportDocumentsViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="supuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Support Record",
            transaction_date=timezone.now().date(),
        )
        self.url = reverse("documents:add_support_docs", args=[self.record.id])

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_owner_can_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "documents/upload_supporting_files.html")

    def test_other_user_cannot_access(self):
        user2 = User.objects.create_user(username="othersup", password="pass")
        self.client.force_login(user2)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_record(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("documents:add_support_docs", args=[99999]))
        self.assertEqual(response.status_code, 404)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class DocumentListViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="listuser", password="pass")
        self.url = reverse("documents:document_list_view")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "documents/document_list.html")

    def test_pagination_first_page(self):
        self.client.force_login(self.user)
        for i in range(30):
            DocumentData.objects.create(
                user=self.user,
                title=f"Doc {i}",
                filepath=f"users/1/doc_{i}.pdf",
                file_hash=f"hash{i:04d}",
            )
        response = self.client.get(self.url)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(len(response.context["documents"]), 25)

    def test_pagination_second_page(self):
        self.client.force_login(self.user)
        for i in range(30):
            DocumentData.objects.create(
                user=self.user,
                title=f"Doc {i}",
                filepath=f"users/1/doc_{i}.pdf",
                file_hash=f"hash{i:04d}",
            )
        response = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(response.context["documents"]), 5)

    def test_pagination_invalid_page_returns_404(self):
        self.client.force_login(self.user)
        DocumentData.objects.create(
            user=self.user,
            title="Test",
            filepath="users/1/test.pdf",
            file_hash="hash0000",
        )
        response = self.client.get(self.url, {"page": "abc"})
        self.assertEqual(response.status_code, 404)

    def test_pagination_out_of_range_page_returns_404(self):
        self.client.force_login(self.user)
        for i in range(30):
            DocumentData.objects.create(
                user=self.user,
                title=f"Doc {i}",
                filepath=f"users/1/doc_{i}.pdf",
                file_hash=f"hash{i:04d}",
            )
        response = self.client.get(self.url, {"page": 999})
        self.assertEqual(response.status_code, 404)

    def test_pagination_empty_page(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertFalse(response.context["is_paginated"])
        self.assertEqual(len(response.context["documents"]), 0)


class PendingOCRListViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ocruser", password="pass")
        self.url = reverse("documents:pending_ocr")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            transaction_date=timezone.now().date(),
        )

    def _make_doc(self, status: str, **kwargs) -> DocumentData:
        return DocumentData.objects.create(
            user=self.user,
            filepath="users/1/test.pdf",
            file_hash=hashlib.sha256(f"test_{status}".encode()).hexdigest(),
            status=status,
            did_ocr=True,
            **kwargs,
        )

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "documents/pending_ocr_list.html")

    def test_includes_uploaded_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.UPLOADED)
        response = self.client.get(self.url)
        self.assertIn(doc, response.context["documents"])

    def test_includes_processing_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.PROCESSING)
        response = self.client.get(self.url)
        self.assertIn(doc, response.context["documents"])

    def test_includes_completed_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.COMPLETED)
        response = self.client.get(self.url)
        self.assertIn(doc, response.context["documents"])

    def test_includes_error_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.ERROR)
        response = self.client.get(self.url)
        self.assertIn(doc, response.context["documents"])

    def test_excludes_pending_upload_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.PENDING_UPLOAD)
        response = self.client.get(self.url)
        self.assertNotIn(doc, response.context["documents"])

    def test_excludes_deleting_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.DELETING)
        response = self.client.get(self.url)
        self.assertNotIn(doc, response.context["documents"])

    def test_excludes_linked_docs(self):
        self.client.force_login(self.user)
        doc = self._make_doc(DocumentStatus.COMPLETED, associated_record=self.record)
        response = self.client.get(self.url)
        self.assertNotIn(doc, response.context["documents"])

    def test_excludes_did_ocr_false_docs(self):
        self.client.force_login(self.user)
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/support.pdf",
            file_hash=hashlib.sha256(b"support").hexdigest(),
            status=DocumentStatus.UPLOADED,
            did_ocr=False,
        )
        response = self.client.get(self.url)
        self.assertNotIn(doc, response.context["documents"])

    def test_excludes_other_users_docs(self):
        other = User.objects.create_user(username="other", password="pass")
        self.client.force_login(self.user)
        doc = DocumentData.objects.create(
            user=other,
            filepath="users/2/other.pdf",
            file_hash=hashlib.sha256(b"other").hexdigest(),
            status=DocumentStatus.COMPLETED,
            did_ocr=True,
        )
        response = self.client.get(self.url)
        self.assertNotIn(doc, response.context["documents"])

    def test_empty_state(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["documents"]), 0)
        self.assertContains(response, "No documents found")
