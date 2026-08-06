"""Tests for the denormalized storage counter (billing.storage + signals).

The counter must stay in sync with the active document set across every
lifecycle transition: create, confirm-size, permanent delete, hard delete,
and bulk ``QuerySet.delete()``.
"""

import pytest
from django.utils import timezone

from billing.models import CustomUser
from billing.storage import (
    adjust_storage_usage,
    get_storage_usage_bytes,
    reconcile_storage_usage,
)
from documents.models import DocumentData, DocumentStatus

from conftest import DocumentDataFactory


@pytest.fixture
def active_doc(user):
    return DocumentDataFactory(user=user, file_size=2048, status=DocumentStatus.UPLOADED)


def _counter(user) -> int:
    return (
        CustomUser.objects.filter(pk=user.pk).values_list("storage_used_bytes", flat=True).first()
    )


def test_fresh_user_has_zero_usage(user):
    assert get_storage_usage_bytes(user) == 0


def test_creating_document_adds_to_counter(user):
    DocumentDataFactory(user=user, file_size=2048)
    assert _counter(user) == 2048


def test_creating_document_without_size_counts_zero(user):
    DocumentDataFactory(user=user, file_size=None)
    assert _counter(user) == 0


def test_setting_file_size_on_save_adjusts_counter(user):
    doc = DocumentDataFactory(user=user, file_size=None)
    doc.file_size = 5120
    doc.save(update_fields=["file_size"])
    assert _counter(user) == 5120


def test_delete_deducts_usage(user):
    doc = DocumentDataFactory(user=user, file_size=4096, did_ocr=True)
    assert _counter(user) == 4096
    doc.delete()
    assert _counter(user) == 0
    assert not DocumentData.objects.filter(pk=doc.pk).exists()


def test_hard_delete_deducts_usage(user):
    doc = DocumentDataFactory(user=user, file_size=4096, did_ocr=False)
    doc.delete()
    assert _counter(user) == 0
    assert not DocumentData.objects.filter(pk=doc.pk).exists()


def test_hard_delete_method_deducts_usage(user):
    doc = DocumentDataFactory(user=user, file_size=4096, did_ocr=True)
    doc.hard_delete()
    assert _counter(user) == 0


def test_bulk_delete_deducts_each_document(user):
    for i in range(3):
        DocumentDataFactory(
            user=user,
            file_size=1000,
            filepath=f"users/1/bulk-{i}.pdf",
            file_hash=f"bulk-{i}",
        )
    DocumentData.objects.filter(user=user).delete()
    assert _counter(user) == 0


def test_counter_never_goes_negative(user):
    DocumentDataFactory(user=user, file_size=100)
    adjust_storage_usage(user.pk, -10_000)
    assert _counter(user) == 0


def test_usage_is_per_user(user, other_user):
    DocumentDataFactory(user=user, file_size=1000)
    assert _counter(user) == 1000
    assert _counter(other_user) == 0


def test_reconcile_recomputes_counter(user):
    doc = DocumentDataFactory(user=user, file_size=2048, did_ocr=True)
    doc.delete()
    adjust_storage_usage(user.pk, 999)
    assert _counter(user) == 999
    corrected = reconcile_storage_usage(user.pk)
    assert corrected == 1
    assert _counter(user) == 0


def test_reconcile_ignores_deleted_documents(user):
    DocumentDataFactory(user=user, file_size=2048, did_ocr=True).delete()
    reconcile_storage_usage(user.pk)
    assert _counter(user) == 0


def test_reconcile_all_users(user, other_user):
    DocumentDataFactory(user=user, file_size=500)
    DocumentDataFactory(user=other_user, file_size=300)
    adjust_storage_usage(user.pk, 100)
    assert reconcile_storage_usage() >= 2
    assert _counter(user) == 500
    assert _counter(other_user) == 300


def test_expired_cleanup_style_queryset_delete_keeps_counter_consistent(user):
    stale = DocumentDataFactory(
        user=user,
        file_size=1024,
        date_added=timezone.now() - timezone.timedelta(days=30),
    )
    DocumentData.objects.filter(pk=stale.pk).delete()
    assert _counter(user) == 0
