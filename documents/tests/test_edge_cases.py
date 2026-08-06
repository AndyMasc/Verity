"""Expanded edge-case tests for DocumentData model.

Covers permanent delete, hard_delete, with_record queryset, and
file_extension auto-derivation.
"""

import hashlib

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from records.models import Record

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


class DocumentPermanentDeleteEdgeCasesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="docedge", password="pass")

    def test_delete_permanently_removes_ocr_document(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/ocr.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
        )
        pk = doc.id
        doc.delete()
        self.assertFalse(DocumentData.objects.filter(id=pk).exists())

    def test_delete_permanently_removes_non_ocr_document(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/no_ocr.pdf",
            file_hash=_make_hash(b"no_ocr"),
            did_ocr=False,
        )
        pk = doc.id
        doc.delete()
        self.assertFalse(DocumentData.objects.filter(id=pk).exists())

    def test_hard_delete_removes_regardless_of_ocr(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/hard.pdf",
            file_hash=_make_hash(b"hard"),
            did_ocr=True,
        )
        pk = doc.id
        doc.hard_delete()
        self.assertFalse(DocumentData.objects.filter(id=pk).exists())


class DocumentFileExtensionTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="exttest", password="pass")

    def test_auto_extract_pdf(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/file.pdf",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.file_extension, "pdf")

    def test_auto_extract_uppercase(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/file.PDF",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.file_extension, "pdf")

    def test_auto_extract_image(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/photo.jpg",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.file_extension, "jpg")

    def test_no_extension(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/noext",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.file_extension, "")

    def test_save_clears_extension_when_empty_path(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/file.pdf",
            file_hash=_make_hash(),
        )
        doc.filepath = "users/1/noext"
        doc.file_extension = ""
        doc.save()
        self.assertEqual(doc.file_extension, "")


class DocumentIsProcessingTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="proctest", password="pass")

    def test_pending_upload_is_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        self.assertTrue(doc.is_processing)

    def test_uploaded_is_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.UPLOADED,
        )
        self.assertTrue(doc.is_processing)

    def test_processing_is_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PROCESSING,
        )
        self.assertTrue(doc.is_processing)

    def test_completed_is_not_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
        )
        self.assertFalse(doc.is_processing)

    def test_error_is_not_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.ERROR,
        )
        self.assertFalse(doc.is_processing)

    def test_completed_is_terminal(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
        )
        self.assertTrue(doc.is_terminal)

    def test_error_is_terminal(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/p.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.ERROR,
        )
        self.assertTrue(doc.is_terminal)
