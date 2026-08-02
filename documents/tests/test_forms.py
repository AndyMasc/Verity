import hashlib

from django.contrib.auth import get_user_model

User = get_user_model()
from django.http import HttpRequest
from django.test import TestCase
from django.utils import timezone

from documents.filters import DocumentFilter
from documents.forms import R2UploadForm, DocumentUpdateForm
from documents.models import DocumentData
from records.models import Record


def _make_hash(content: bytes = b"test content") -> str:
    return hashlib.sha256(content).hexdigest()


def _make_doc_filter_request(user):
    req = HttpRequest()
    req.user = user
    return req


class R2UploadFormTest(TestCase):
    def test_valid_form(self):
        form = R2UploadForm(
            data={
                "filename": "test.pdf",
                "content_type": "application/pdf",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["filename"], "test.pdf")

    def test_filename_cleaned_to_basename(self):
        form = R2UploadForm(
            data={
                "filename": "subdir/test.pdf",
                "content_type": "application/pdf",
            }
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["filename"], "test.pdf")

    def test_dot_filename_rejected(self):
        form = R2UploadForm(
            data={
                "filename": ".",
                "content_type": "application/pdf",
            }
        )
        self.assertFalse(form.is_valid())

    def test_dotdot_filename_rejected(self):
        form = R2UploadForm(
            data={
                "filename": "..",
                "content_type": "application/pdf",
            }
        )
        self.assertFalse(form.is_valid())

    def test_empty_filename(self):
        form = R2UploadForm(data={"filename": "", "content_type": "application/pdf"})
        self.assertFalse(form.is_valid())

    def test_missing_content_type(self):
        form = R2UploadForm(data={"filename": "test.pdf"})
        self.assertFalse(form.is_valid())

    def test_allowed_content_types(self):
        for ct in [
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/heic",
            "image/heif",
        ]:
            with self.subTest(ct=ct):
                form = R2UploadForm(
                    data={
                        "filename": f"test.{ct.split('/')[1]}",
                        "content_type": ct,
                    }
                )
                self.assertTrue(form.is_valid(), msg=f"Failed for {ct}")

    def test_disallowed_content_type(self):
        form = R2UploadForm(
            data={
                "filename": "test.txt",
                "content_type": "text/plain",
            }
        )
        self.assertFalse(form.is_valid())

    def test_notes_field_optional(self):
        form = R2UploadForm(
            data={
                "filename": "test.pdf",
                "content_type": "application/pdf",
                "notes": "Some notes",
            }
        )
        self.assertTrue(form.is_valid())


class DocumentUpdateFormTest(TestCase):
    def test_valid_data(self):
        form = DocumentUpdateForm(
            data={
                "title": "Updated Title",
                "notes": "Some notes",
            }
        )
        self.assertTrue(form.is_valid())

    def test_associated_record_not_required(self):
        form = DocumentUpdateForm(data={"title": "Just Title"})
        self.assertTrue(form.is_valid())
        self.assertFalse(form.fields["associated_record"].required)

    def test_empty_title(self):
        form = DocumentUpdateForm(data={"title": ""})
        self.assertFalse(form.is_valid())

    def test_associated_record_queryset_active_only(self):
        user = User.objects.create_user(username="formuser", password="pass")
        active = Record.objects.create(
            user=user,
            title="Active",
            record_type="expense_receipt",
            transaction_date=timezone.now().date(),
        )
        inactive = Record.objects.create(
            user=user,
            title="Inactive",
            record_type="voucher",
            is_active=False,
            transaction_date=timezone.now().date(),
        )
        form = DocumentUpdateForm()
        qs = form.fields["associated_record"].queryset
        self.assertIn(active, qs)
        self.assertNotIn(inactive, qs)


class DocumentFilterTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            record_type="expense_receipt",
            transaction_date=timezone.now().date(),
        )

    def test_filter_by_status_orphaned(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/report.pdf",
            file_hash=_make_hash(),
        )
        DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/linked.pdf",
            file_hash=_make_hash(b"linked"),
        )
        qs = DocumentData.objects.filter(user=self.user)
        f = DocumentFilter(
            {"status": "orphaned"},
            queryset=qs,
            request=_make_doc_filter_request(self.user),
        )
        self.assertEqual(f.qs.count(), 1)

    def test_filter_by_status_linked(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/report.pdf",
            file_hash=_make_hash(),
        )
        DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/linked.pdf",
            file_hash=_make_hash(b"linked"),
        )
        qs = DocumentData.objects.filter(user=self.user)
        f = DocumentFilter(
            {"status": "linked"},
            queryset=qs,
            request=_make_doc_filter_request(self.user),
        )
        self.assertEqual(f.qs.count(), 1)

    def test_filter_by_file_type_queryset(self):
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/report.pdf",
            file_hash=_make_hash(),
            file_extension="pdf",
        )
        DocumentData.objects.create(
            user=self.user,
            filepath="users/1/photo.jpg",
            file_hash=_make_hash(b"photo"),
            file_extension="jpg",
        )
        qs = DocumentData.objects.filter(user=self.user, file_extension__iexact="pdf")
        self.assertEqual(qs.count(), 1)

    def test_no_filters(self):
        DocumentData.objects.create(
            user=self.user, filepath="users/1/a.pdf", file_hash=_make_hash()
        )
        qs = DocumentData.objects.filter(user=self.user)
        f = DocumentFilter({}, queryset=qs, request=_make_doc_filter_request(self.user))
        self.assertEqual(f.qs.count(), 1)
