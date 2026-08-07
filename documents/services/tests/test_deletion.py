"""Tests for the DocumentDeletionService.

Covers soft_delete and hard_delete, both of which permanently remove documents.
"""

import hashlib
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from documents.models import DocumentData
from documents.services.deletion import DocumentDeletionService

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestSoftDelete:
    def test_permanently_deletes_document(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.success is True
        assert result.message_tag == "success"
        assert not DocumentData.objects.filter(id=doc.id).exists()

    def test_deletes_even_with_ocr_data(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
            ocr_raw_data={"title": "Receipt"},
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.success is True
        assert not DocumentData.objects.filter(id=doc.id).exists()

    def test_soft_delete_returns_filepath(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.filepath == "users/1/doc.pdf"

    def test_soft_delete_returns_record_id(self, user):
        from records.models import Record

        record = Record.objects.create(
            user=user,
            title="Rec",
            record_type="expense_receipt",
            transaction_date=timezone.now().date(),
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            associated_record=record,
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.record_id == record.id

    def test_soft_delete_handles_exception(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        with patch.object(DocumentData, "delete", side_effect=Exception("DB down")):
            result = DocumentDeletionService.soft_delete(doc)
            assert result.success is False
            assert "system error" in result.error.lower()


@pytest.mark.django_db
class TestHardDelete:
    def test_permanently_removes_document(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        result = DocumentDeletionService.hard_delete(doc)
        assert result.success is True
        assert result.filepath == "users/1/doc.pdf"
        assert not DocumentData.objects.filter(id=doc.id).exists()
