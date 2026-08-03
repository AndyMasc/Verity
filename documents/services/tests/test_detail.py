"""Tests for the DocumentDetailService.

Covers build_context, _search_records, associate_record, and pagination.
"""

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.http import HttpRequest, QueryDict
from django.test import RequestFactory

from documents.models import DocumentData, DocumentStatus
from documents.services.detail import DocumentDetailService
from records.models import Record

User = get_user_model()


def _make_hash(content: bytes = b"test") -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.mark.django_db
class TestBuildContext:
    @patch(
        "documents.services.detail.generate_read_presigned_url",
        return_value="https://view.url",
    )
    def test_returns_document_context(self, mock_url, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        factory = RequestFactory()
        request = factory.get("/documents/1/")
        request.user = user
        ctx = DocumentDetailService.build_context(doc, request)
        assert ctx.view_url == "https://view.url"
        assert ctx.seven_years_ago_unix > 0
        assert ctx.is_paginated is False

    @patch(
        "documents.services.detail.generate_read_presigned_url",
        return_value="https://view.url",
    )
    def test_search_filters_by_user(self, mock_url, user):
        other = User.objects.create_user(username="other", password="pass")
        Record.objects.create(
            user=user,
            title="My Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        Record.objects.create(
            user=other,
            title="Other Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        factory = RequestFactory()
        request = factory.get("/documents/1/")
        request.user = user
        ctx = DocumentDetailService.build_context(doc, request)
        assert len(ctx.records) == 1


@pytest.mark.django_db
class TestAssociateRecord:
    def test_sets_record(self, user):
        record = Record.objects.create(
            user=user,
            title="Rec",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        DocumentDetailService.associate_record(doc, str(record.id), user)
        doc.save()
        doc.refresh_from_db()
        assert doc.associated_record == record

    def test_clears_record_with_empty_string(self, user):
        record = Record.objects.create(
            user=user,
            title="Rec",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
            associated_record=record,
        )
        DocumentDetailService.associate_record(doc, "", user)
        doc.save()
        doc.refresh_from_db()
        assert doc.associated_record is None

    def test_raises_404_for_nonexistent_record(self, user):
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        from django.http import Http404

        with pytest.raises(Http404):
            DocumentDetailService.associate_record(doc, "99999", user)

    def test_raises_404_for_other_users_record(self, user):
        other = User.objects.create_user(username="other", password="pass")
        record = Record.objects.create(
            user=other,
            title="Not mine",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        doc = DocumentData.objects.create(
            user=user,
            filepath="users/1/doc.pdf",
            file_hash=_make_hash(),
        )
        from django.http import Http404

        with pytest.raises(Http404):
            DocumentDetailService.associate_record(doc, str(record.id), user)
