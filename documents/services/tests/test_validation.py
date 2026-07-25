"""Tests for the DocumentUploadService validation and confirmation flow.

Covers validate(), confirm(), status gate, key mismatch, R2 not found,
gatekeeper rejection, and successful confirmation.
"""

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model

from documents.models import DocumentData, DocumentStatus
from documents.services.validation import DocumentUploadService, UploadResult

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestDocumentUploadServiceValidate:
    def test_rejects_non_pending_status(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
        )
        svc = DocumentUploadService(doc, "users/1/doc.pdf")
        result = svc.validate()
        assert result.valid is False
        assert result.status_code == 409
        assert "Unexpected status" in result.error

    def test_rejects_key_mismatch(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/wrong.pdf")
        result = svc.validate()
        assert result.valid is False
        assert result.status_code == 400
        assert "Key mismatch" in result.error

    @patch("documents.services.validation.verify_r2_object_exists", return_value=False)
    def test_sets_error_when_r2_not_found(self, mock_verify, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/doc.pdf")
        result = svc.validate()
        assert result.valid is False
        assert result.status_code == 404
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR

    @patch("documents.services.validation.get_r2_object_head")
    @patch(
        "documents.services.validation.gatekeeper_validate_r2_object",
        return_value={"valid": False, "error": "Bad file"},
    )
    @patch("documents.services.validation.verify_r2_object_exists", return_value=True)
    def test_rejects_gatekeeper_failure(self, mock_verify, mock_gk, mock_head, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/doc.pdf")
        result = svc.validate()
        assert result.valid is False
        assert result.status_code == 422
        assert "Bad file" in result.error
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR

    @patch("documents.services.validation.get_r2_object_head")
    @patch(
        "documents.services.validation.gatekeeper_validate_r2_object",
        return_value={"valid": True},
    )
    @patch("documents.services.validation.verify_r2_object_exists", return_value=True)
    def test_validate_success_returns_metadata(self, mock_verify, mock_gk, mock_head, user):
        mock_head.return_value = {"ContentLength": 1234, "ContentType": "application/pdf"}
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/doc.pdf")
        result = svc.validate()
        assert result.valid is True
        assert result.file_size == 1234
        assert result.mime_type == "application/pdf"
        assert result.document is None


@pytest.mark.django_db
class TestDocumentUploadServiceConfirm:
    @patch("documents.services.validation.get_r2_object_head")
    @patch(
        "documents.services.validation.gatekeeper_validate_r2_object",
        return_value={"valid": True},
    )
    @patch("documents.services.validation.verify_r2_object_exists", return_value=True)
    def test_confirm_transitions_to_uploaded(self, mock_verify, mock_gk, mock_head, user):
        mock_head.return_value = {"ContentLength": 500, "ContentType": "image/jpeg"}
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/doc.pdf")
        result = svc.confirm()
        assert result.valid is True
        assert result.document is not None
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.UPLOADED

    def test_confirm_rejects_key_mismatch(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PENDING_UPLOAD,
        )
        svc = DocumentUploadService(doc, "users/1/other.pdf")
        result = svc.confirm()
        assert result.valid is False
        assert result.status_code == 400

    def test_confirm_strips_semicolon_from_content_type(self, user):
        mock_head = MagicMock(
            return_value={"ContentLength": 100, "ContentType": "image/png; charset=utf-8"}
        )
        with (
            patch("documents.services.validation.get_r2_object_head", mock_head),
            patch(
                "documents.services.validation.gatekeeper_validate_r2_object",
                return_value={"valid": True},
            ),
            patch("documents.services.validation.verify_r2_object_exists", return_value=True),
        ):
            doc = DocumentData.objects.create(
                user=user,
                filepath="users/1/doc.png",
                file_hash=_make_hash(),
                status=DocumentStatus.PENDING_UPLOAD,
            )
            svc = DocumentUploadService(doc, "users/1/doc.png")
            result = svc.validate()
            assert result.mime_type == "image/png"
