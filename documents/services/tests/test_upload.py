"""Tests for the UploadService and _parse_request_data.

Covers parse_request_data, duplicate detection, presigned URL generation,
force_upload, and _resolve_hash.
"""

import hashlib
import uuid
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.http import HttpRequest, QueryDict
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from documents.services.upload import UploadService, _parse_request_data

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestParseRequestData:
    def test_json_body(self):
        request = HttpRequest()
        request.content_type = "application/json"
        request._body = b'{"filename": "test.pdf", "file_hash": "abc123"}'
        data = _parse_request_data(request)
        assert data["filename"] == "test.pdf"

    def test_invalid_json_returns_empty(self):
        request = HttpRequest()
        request.content_type = "application/json"
        request._body = b"not json"
        data = _parse_request_data(request)
        assert data == {}

    def test_form_post(self):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=test.pdf&file_hash=abc123")
        data = _parse_request_data(request)
        assert data["filename"] == "test.pdf"


@pytest.mark.django_db
class TestResolveHash:
    def test_returns_original_when_not_forced(self):
        h = _make_hash()
        result = UploadService._resolve_hash(h, force_upload=False)
        assert result == h

    def test_returns_salted_hash_when_forced(self):
        h = _make_hash()
        result = UploadService._resolve_hash(h, force_upload=True)
        assert result != h
        assert len(result) == 64  # SHA-256 hex digest

    def test_forced_hash_is_deterministic_per_call(self):
        h = _make_hash()
        r1 = UploadService._resolve_hash(h, force_upload=True)
        r2 = UploadService._resolve_hash(h, force_upload=True)
        assert r1 != r2  # UUID salt makes each unique


@pytest.mark.django_db
class TestUploadServiceHandle:
    def test_missing_file_hash_returns_error(self, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=test.pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "error"
        assert "file_hash" in result.error

    def test_missing_filename_returns_error(self, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("file_hash=abc123")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "error"

    def test_invalid_content_type_returns_error(self, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=test.pdf&file_hash=abc123&content_type=text/plain")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "error"

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_new_upload_returns_url(self, mock_presign, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=test.pdf&file_hash=abc123&content_type=application/pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "upload_url"
        assert result.upload_url == "https://upload.url"
        assert result.document_id is not None

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_duplicate_detection(self, mock_presign, user):
        h = _make_hash()
        DocumentData.objects.create(
            user=user,
            filepath="users/1/existing.pdf",
            file_hash=h,
        )
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict(f"filename=dup.pdf&file_hash={h}&content_type=application/pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "duplicate_confirmed"

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_force_upload_skips_duplicate(self, mock_presign, user):
        h = _make_hash()
        DocumentData.objects.create(
            user=user,
            filepath="users/1/existing.pdf",
            file_hash=h,
        )
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict(
            f"filename=force.pdf&file_hash={h}&content_type=application/pdf&force_upload=true"
        )
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "upload_url"

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_creates_document_with_correct_status(self, mock_presign, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=new.pdf&file_hash=abc123&content_type=application/pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        doc = DocumentData.objects.get(id=result.document_id)
        assert doc.status == DocumentStatus.PENDING_UPLOAD

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_sets_did_ocr_when_no_record(self, mock_presign, user):
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict("filename=new.pdf&file_hash=abc123&content_type=application/pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        doc = DocumentData.objects.get(id=result.document_id)
        assert doc.did_ocr is True

    @patch("documents.services.upload.generate_presigned_post", return_value="https://upload.url")
    def test_duplicate_result_includes_record_info(self, mock_presign, user):
        from records.models import Record

        record = Record.objects.create(
            user=user,
            title="Test Record",
            record_type="expense_receipt",
            transaction_date=timezone.now().date(),
        )
        h = _make_hash()
        DocumentData.objects.create(
            user=user,
            filepath="users/1/linked.pdf",
            file_hash=h,
            associated_record=record,
        )
        request = HttpRequest()
        request.content_type = "application/x-www-form-urlencoded"
        request.POST = QueryDict(f"filename=dup.pdf&file_hash={h}&content_type=application/pdf")
        request.user = user
        svc = UploadService(request)
        result = svc.handle()
        assert result.status == "duplicate_confirmed"
        assert result.existing_record_id == record.id
        assert result.existing_record_label == "Test Record"
