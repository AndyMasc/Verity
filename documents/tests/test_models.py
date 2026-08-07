import hashlib

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from records.models import Record


def _make_hash(content: bytes = b"test content") -> str:
    return hashlib.sha256(content).hexdigest()


class DocumentDataModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            transaction_date=timezone.now().date(),
        )

    def test_create_document_data(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/uuid-test.pdf",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.status, DocumentStatus.PENDING_UPLOAD)
        self.assertEqual(doc.file_extension, "pdf")
        self.assertEqual(doc.title, "Untitled")
        self.assertIsNotNone(doc.date_added)

    def test_create_document_data_with_record(self):
        doc = DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/uuid-test.pdf",
            file_hash=_make_hash(),
        )
        self.assertEqual(doc.associated_record, self.record)

    def test_file_extension_auto_extracted_on_save(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/uuid-document.PDF",
            file_hash=_make_hash(),
        )
        doc.save()
        self.assertEqual(doc.file_extension, "pdf")

    def test_file_extension_no_extension(self):
        doc = DocumentData(
            user=self.user,
            filepath="users/1/noext",
            file_hash=_make_hash(),
        )
        doc.save()
        self.assertEqual(doc.file_extension, "")

    def test_str(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/test.pdf",
            file_hash=_make_hash(),
        )
        self.assertEqual(str(doc), "users/1/test.pdf")

    def test_status_transitions_processing(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/test.pdf",
            file_hash=_make_hash(),
        )
        self.assertTrue(doc.is_processing)
        self.assertFalse(doc.is_terminal)

        doc.status = DocumentStatus.UPLOADED
        doc.save()
        self.assertTrue(doc.is_processing)
        self.assertFalse(doc.is_terminal)

        doc.status = DocumentStatus.PROCESSING
        doc.save()
        self.assertTrue(doc.is_processing)
        self.assertFalse(doc.is_terminal)

    def test_status_transitions_terminal(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/test.pdf",
            file_hash=_make_hash(),
        )
        doc.status = DocumentStatus.COMPLETED
        doc.save()
        self.assertFalse(doc.is_processing)
        self.assertTrue(doc.is_terminal)

        doc.status = DocumentStatus.ERROR
        doc.save()
        self.assertFalse(doc.is_processing)
        self.assertTrue(doc.is_terminal)

    def test_status_choices(self):
        self.assertEqual(DocumentStatus.PENDING_UPLOAD, "pending_upload")
        self.assertEqual(DocumentStatus.UPLOADED, "uploaded")
        self.assertEqual(DocumentStatus.PROCESSING, "processing")
        self.assertEqual(DocumentStatus.COMPLETED, "completed")
        self.assertEqual(DocumentStatus.ERROR, "error")
        self.assertEqual(DocumentStatus.DELETING, "deleting")

    def test_queryset_for_user(self):
        user2 = User.objects.create_user(username="user2", password="pass")
        DocumentData.objects.create(
            user=self.user, filepath="users/1/mine.pdf", file_hash=_make_hash()
        )
        DocumentData.objects.create(
            user=user2, filepath="users/2/theirs.pdf", file_hash=_make_hash(b"other")
        )
        self.assertEqual(DocumentData.objects.for_user(self.user).count(), 1)
        self.assertEqual(DocumentData.objects.for_user(user2).count(), 1)

    def test_queryset_orphaned(self):
        DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/linked.pdf",
            file_hash=_make_hash(),
        )
        orphan = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/orphan.pdf",
            file_hash=_make_hash(b"orphan"),
        )
        qs = DocumentData.objects.orphaned()
        self.assertIn(orphan, qs)
        self.assertEqual(qs.count(), 1)

    def test_queryset_linked(self):
        DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/linked.pdf",
            file_hash=_make_hash(),
        )
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/orphan.pdf",
            file_hash=_make_hash(b"orphan"),
        )
        qs = DocumentData.objects.linked()
        self.assertEqual(qs.count(), 1)

    def test_queryset_by_status(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/ready.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
        )
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/error.pdf",
            file_hash=_make_hash(b"err"),
            status=DocumentStatus.ERROR,
        )
        self.assertEqual(DocumentData.objects.by_status("completed").count(), 1)
        self.assertEqual(DocumentData.objects.by_status("error").count(), 1)

    def test_queryset_pending(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/pending.pdf",
            file_hash=_make_hash(),
        )
        self.assertEqual(DocumentData.objects.pending().count(), 1)

    def test_queryset_stale_pending(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/stale.pdf",
            file_hash=_make_hash(),
        )
        DocumentData.objects.filter(pk=doc.pk).update(
            date_added=timezone.now() - timezone.timedelta(hours=2)
        )
        self.assertTrue(DocumentData.objects.stale_pending().exists())

    def test_queryset_stale_error(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/err.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.ERROR,
        )
        DocumentData.objects.filter(pk=doc.pk).update(
            date_added=timezone.now() - timezone.timedelta(days=3)
        )
        self.assertTrue(DocumentData.objects.stale_error().exists())

    def test_queryset_search(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/tax.pdf",
            file_hash=_make_hash(),
            title="Tax Document",
        )
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/receipt.pdf",
            file_hash=_make_hash(b"receipt"),
            title="Receipt",
        )
        qs = DocumentData.objects.search("tax")
        self.assertEqual(qs.count(), 1)

    def test_queryset_search_empty(self):
        qs = DocumentData.objects.search("")
        self.assertEqual(qs.count(), 0)

    def test_unique_constraint(self):
        h = _make_hash()
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/unique.pdf",
            file_hash=h,
        )
        with self.assertRaises(Exception):
            DocumentData.objects.create(
                user=self.user,
                filepath="users/1/duplicate.pdf",
                file_hash=h,
            )

    def test_unique_constraint_different_user(self):
        user2 = User.objects.create_user(username="user2", password="pass")
        h = _make_hash()
        DocumentData.objects.create(user=self.user, filepath="users/1/a.pdf", file_hash=h)
        doc = DocumentData.objects.create(user=user2, filepath="users/2/a.pdf", file_hash=h)
        self.assertIsNotNone(doc.pk)


class DocumentDataManagerTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            transaction_date=timezone.now().date(),
        )

    def test_orphaned_excludes_linked(self):
        linked = DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/linked.pdf",
            file_hash=_make_hash(),
        )
        orphan = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/orphan.pdf",
            file_hash=_make_hash(b"orphan"),
        )
        qs = DocumentData.objects.orphaned()
        self.assertIn(orphan, qs)
        self.assertNotIn(linked, qs)
