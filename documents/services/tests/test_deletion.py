"""Tests for the DocumentDeletionService.

Covers soft_delete, undo_delete, hard_delete, and is_eligible_for_hard_delete.
"""

import hashlib
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from documents.models import DocumentData, DocumentStatus
from documents.services.cleanup import COMPLIANCE_RETENTION_YEARS
from documents.services.deletion import DocumentDeletionService

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestSoftDelete:
    def test_non_ocr_hard_deletes_immediately(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=False,
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.success is True
        assert result.message_tag == "success"
        assert not DocumentData.objects.filter(id=doc.id).exists()

    def test_ocr_soft_deletes_for_compliance(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
        )
        result = DocumentDeletionService.soft_delete(doc)
        assert result.success is True
        assert result.message_tag == "info"
        assert "compliance" in result.message.lower()
        doc.refresh_from_db()
        assert doc.is_active is False
        assert doc.deleted_at is not None

    def test_soft_delete_returns_filepath(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=False,
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
            did_ocr=False,
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
class TestUndoDelete:
    def test_restores_soft_deleted_document(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            did_ocr=True,
        )
        doc.delete()
        doc.refresh_from_db()
        assert doc.is_active is False
        result = DocumentDeletionService.undo_delete(doc)
        assert result.success is True
        doc.refresh_from_db()
        assert doc.is_active is True
        assert doc.deleted_at is None


@pytest.mark.django_db
class TestIsEligibleForHardDelete:
    def test_eligible_after_7_years(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        DocumentData.objects.filter(id=doc.id).update(
            date_added=timezone.now() - timedelta(days=365 * COMPLIANCE_RETENTION_YEARS + 1)
        )
        doc.refresh_from_db()
        assert DocumentDeletionService.is_eligible_for_hard_delete(doc) is True

    def test_not_eligible_recent(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        assert DocumentDeletionService.is_eligible_for_hard_delete(doc) is False


@pytest.mark.django_db
class TestHardDelete:
    def test_permently_removes_document(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        result = DocumentDeletionService.hard_delete(doc)
        assert result.success is True
        assert result.filepath == "users/1/doc.pdf"
        assert not DocumentData.objects.filter(id=doc.id).exists()
