"""Tests for the OCR pipeline service.

Covers get_cache_key, set_document_status, mark_ocr_failed,
fetch_from_r2, and extract (with mocked Gemini).
"""

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from dramatiq import Message

from documents.models import DocumentData, DocumentStatus
from documents.services.ocr import (
    MAX_OCR_RETRIES,
    extract,
    get_cache_key,
    mark_ocr_failed,
    set_document_status,
)

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


def _retry_message(retries: int) -> Message:
    return Message(
        queue_name="default",
        actor_name="extract_document",
        args=(1,),
        kwargs={},
        options={"retries": retries},
    )


class TestGetCacheKey:
    def test_returns_expected_format(self):
        assert get_cache_key(42) == "ocr_status_42"

    def test_returns_string(self):
        assert isinstance(get_cache_key(1), str)


@pytest.mark.django_db
class TestSetDocumentStatus:
    def test_updates_status(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        set_document_status(doc.id, DocumentStatus.PROCESSING)
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.PROCESSING

    def test_updates_additional_fields(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        set_document_status(doc.id, DocumentStatus.ERROR, ocr_error="timeout")
        doc.refresh_from_db()
        assert doc.ocr_error == "timeout"


@pytest.mark.django_db
class TestMarkOcrFailed:
    def test_marks_document_error_and_caches_payload(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PROCESSING,
        )
        mark_ocr_failed(doc.id, "Gemini timeout")
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR
        assert doc.ocr_error == "Gemini timeout"
        assert "error" in cache.get(get_cache_key(doc.id))


class TestRenderPdfPages:
    def test_returns_none_for_invalid_pdf_bytes(self):
        from documents.ocr_helpers import render_pdf_pages

        assert render_pdf_pages(b"%PDF-1.4 not a real pdf") is None

    def test_renders_pdf_to_jpeg_pages(self):
        from io import BytesIO

        from PIL import Image

        from documents.ocr_helpers import render_pdf_pages

        img = Image.new("RGB", (400, 300), "white")
        buffer = BytesIO()
        img.save(buffer, format="PDF")
        pages = render_pdf_pages(buffer.getvalue())
        assert pages is not None
        assert len(pages) == 1
        assert pages[0][:3] == b"\xff\xd8\xff"  # JPEG magic bytes


@pytest.mark.django_db
class TestExtract:
    def test_returns_error_for_nonexistent_doc(self):
        result = extract(99999)
        assert "error" in result

    def test_returns_already_processed_when_did_ocr_true(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
        )
        cache.delete(get_cache_key(doc.id))
        result = extract(doc.id)
        assert "error" in result
        assert "Already processed" in result["error"]

    @patch("documents.services.ocr.cache.get", return_value={"title": "Cached"})
    def test_returns_cached_result_when_available(self, mock_cache_get, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
        )
        result = extract(doc.id)
        assert result["title"] == "Cached"

    @patch("documents.services.ocr.fetch_from_r2", return_value=b"%PDF-1.4 valid")
    @patch("documents.services.ocr.validate_uploaded_bytes", return_value=None)
    @patch("documents.services.ocr.process_image")
    @patch("documents.services.ocr.call_gemini", return_value={"title": "Receipt"})
    def test_full_pipeline_success(self, mock_gemini, mock_process, mock_validate, mock_r2, user):
        mock_part = MagicMock()
        mock_process.return_value = mock_part
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.UPLOADED,
        )
        result = extract(doc.id)
        assert result["title"] == "Receipt"
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.COMPLETED
        assert doc.did_ocr is True

    @patch("documents.services.ocr.fetch_from_r2", return_value=b"not a valid file")
    @patch("documents.services.ocr.call_gemini")
    def test_rejects_invalid_bytes_without_gemini(self, mock_gemini, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.UPLOADED,
        )
        result = extract(doc.id)
        assert "error" in result
        assert "Unable to validate" in result["error"] or "not allowed" in result["error"]
        mock_gemini.assert_not_called()
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR

    @patch("documents.services.ocr.fetch_from_r2", side_effect=Exception("R2 error"))
    def test_failure_marks_error_outside_worker(self, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PROCESSING,
        )
        with pytest.raises(Exception, match="R2 error"):
            extract(doc.id)
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR

    @patch("documents.services.ocr.fetch_from_r2", side_effect=Exception("R2 error"))
    @patch(
        "documents.services.ocr.CurrentMessage.get_current_message",
        return_value=_retry_message(retries=0),
    )
    def test_retryable_failure_reraises_without_marking_error(self, mock_msg, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PROCESSING,
        )
        with pytest.raises(Exception, match="R2 error"):
            extract(doc.id)
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.PROCESSING

    @patch("documents.services.ocr.fetch_from_r2", side_effect=Exception("R2 error"))
    @patch(
        "documents.services.ocr.CurrentMessage.get_current_message",
        return_value=_retry_message(retries=MAX_OCR_RETRIES),
    )
    def test_final_retry_marks_error(self, mock_msg, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.PROCESSING,
        )
        with pytest.raises(Exception, match="R2 error"):
            extract(doc.id)
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR
