"""Tests for the OCR pipeline service.

Covers get_cache_key, set_document_status, increment_ocr_retries,
fetch_from_r2, and extract (with mocked Gemini).
"""

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from documents.models import DocumentData, DocumentStatus
from documents.services.ocr import (
    GeminiOCRError,
    extract,
    get_cache_key,
    increment_ocr_retries,
    set_document_status,
)

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


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
class TestIncrementOcrRetries:
    def test_increments_from_zero(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        result = increment_ocr_retries(doc.id)
        assert result == 1

    def test_increments_from_existing(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            ocr_retries=2,
        )
        result = increment_ocr_retries(doc.id)
        assert result == 3

    def test_returns_zero_for_nonexistent(self):
        result = increment_ocr_retries(99999)
        assert result == 0


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

    @patch("documents.services.ocr.fetch_from_r2", return_value=b"image content")
    @patch("documents.services.ocr.process_image")
    @patch("documents.services.ocr.call_gemini", return_value={"title": "Receipt"})
    def test_full_pipeline_success(self, mock_gemini, mock_process, mock_r2, user):
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

    @patch("documents.services.ocr.fetch_from_r2", side_effect=Exception("R2 error"))
    def test_failure_increments_retries(self, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.UPLOADED,
        )
        with pytest.raises(Exception):
            extract(doc.id)
        doc.refresh_from_db()
        assert doc.ocr_retries == 1

    @patch("documents.services.ocr.fetch_from_r2", side_effect=Exception("R2 error"))
    def test_max_retries_raises_gemini_error(self, mock_r2, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.UPLOADED,
            ocr_retries=3,
        )
        with pytest.raises(GeminiOCRError):
            extract(doc.id)
        doc.refresh_from_db()
        assert doc.status == DocumentStatus.ERROR
