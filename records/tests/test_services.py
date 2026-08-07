"""Tests for the records service layer.

Covers archive_record, unarchive_record, bulk_toggle_archive,
create_record_from_ocr, kickoff_ocr_scan, and BulkLimitExceededError
boundary conditions.
"""

import hashlib
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model

User = get_user_model()
from django.core.cache import cache
from django.test import TestCase

from core.currencies import DEFAULT_CURRENCY
from documents.models import DocumentData, DocumentStatus
from records.models import AuditLog, Record
from records.services import (
    BULK_LIMIT,
    BulkLimitExceededError,
    archive_record,
    bulk_toggle_archive,
    create_record_from_ocr,
    kickoff_ocr_scan,
    unarchive_record,
)


class ArchiveRecordTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.record = Record.objects.create(
            user=self.user, title="Test", record_type="expense_receipt"
        )

    def test_archive_sets_inactive(self):
        archive_record(self.user, self.record)
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)

    def test_unarchive_sets_active(self):
        self.record.is_active = False
        self.record.save()
        unarchive_record(self.user, self.record)
        self.record.refresh_from_db()
        self.assertTrue(self.record.is_active)


class BulkToggleArchiveTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.other_user = User.objects.create_user(username="other", password="pass")
        self.records = [
            Record.objects.create(user=self.user, title=f"Rec {i}", record_type="expense_receipt")
            for i in range(5)
        ]

    def test_bulk_archive(self):
        ids = [r.id for r in self.records[:3]]
        count = bulk_toggle_archive(ids, self.user, archive=True)
        self.assertEqual(count, 3)
        for r in self.records[:3]:
            r.refresh_from_db()
            self.assertFalse(r.is_active)
        for r in self.records[3:]:
            r.refresh_from_db()
            self.assertTrue(r.is_active)

    def test_bulk_unarchive(self):
        for r in self.records:
            r.is_active = False
            r.save()
        ids = [r.id for r in self.records[:2]]
        count = bulk_toggle_archive(ids, self.user, archive=False)
        self.assertEqual(count, 2)
        self.records[0].refresh_from_db()
        self.assertTrue(self.records[0].is_active)
        self.records[2].refresh_from_db()
        self.assertFalse(self.records[2].is_active)

    def test_skips_already_archived(self):
        self.records[0].is_active = False
        self.records[0].save()
        ids = [r.id for r in self.records[:3]]
        count = bulk_toggle_archive(ids, self.user, archive=True)
        self.assertEqual(count, 2)

    def test_skips_other_users_records(self):
        other_record = Record.objects.create(
            user=self.other_user, title="Other", record_type="expense_receipt"
        )
        ids = [self.records[0].id, other_record.id]
        count = bulk_toggle_archive(ids, self.user, archive=True)
        self.assertEqual(count, 1)
        other_record.refresh_from_db()
        self.assertTrue(other_record.is_active)

    def test_empty_list_returns_zero(self):
        count = bulk_toggle_archive([], self.user, archive=True)
        self.assertEqual(count, 0)

    def test_creates_audit_logs(self):
        ids = [r.id for r in self.records[:2]]
        bulk_toggle_archive(ids, self.user, archive=True)
        log_count = AuditLog.objects.filter(user=self.user, action=AuditLog.Action.ARCHIVE).count()
        self.assertEqual(log_count, 2)

    def test_raises_on_limit_exceeded(self):
        ids = list(range(1, BULK_LIMIT + 2))
        with self.assertRaises(BulkLimitExceededError):
            bulk_toggle_archive(ids, self.user, archive=True)

    def test_exact_limit_succeeds(self):
        ids = [r.id for r in self.records]
        count = bulk_toggle_archive(ids, self.user, archive=True)
        self.assertEqual(count, 5)

    def test_returns_zero_for_nonexistent_ids(self):
        count = bulk_toggle_archive([99999], self.user, archive=True)
        self.assertEqual(count, 0)

    def test_transaction_rolls_back_on_error(self):
        ids = [r.id for r in self.records[:2]]
        with self.assertRaises(BulkLimitExceededError):
            bulk_toggle_archive(list(range(1, BULK_LIMIT + 2)) + ids, self.user, archive=True)
        for r in self.records[:2]:
            r.refresh_from_db()
            self.assertTrue(r.is_active)


def _make_hash(content: bytes = b"ocr") -> str:
    return hashlib.sha256(content).hexdigest()


class CreateRecordFromOCRTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ocrsvc", password="pass")

    def _doc(self, data=None, **kwargs):
        return DocumentData.objects.create(
            user=self.user,
            filepath="users/1/ocr.pdf",
            file_hash=_make_hash(),
            status=DocumentStatus.COMPLETED,
            ocr_raw_data=data,
            **kwargs,
        )

    def test_creates_record_from_persisted_data(self):
        doc = self._doc(
            {
                "title": "Coffee Shop",
                "merchant": "Coffee Shop Inc",
                "balance": 12.5,
                "currency": "usd",
                "transaction_date": "2024-06-15",
                "products": ["Latte", "Croissant"],
                "record_type": "expense_receipt",
            }
        )
        record = create_record_from_ocr(doc.id)
        self.assertIsNotNone(record)
        doc.refresh_from_db()
        self.assertEqual(doc.associated_record, record)
        record.refresh_from_db()
        self.assertEqual(record.title, "Coffee Shop")
        self.assertEqual(record.merchant, "Coffee Shop Inc")
        self.assertEqual(record.balance, Decimal("12.5"))
        self.assertEqual(record.transaction_date, date(2024, 6, 15))
        self.assertEqual(record.currency, "usd")

    def test_is_idempotent(self):
        doc = self._doc({"title": "Coffee"})
        first = create_record_from_ocr(doc.id)
        second = create_record_from_ocr(doc.id)
        self.assertEqual(first, second)
        self.assertEqual(Record.objects.filter(user=self.user).count(), 1)

    def test_returns_existing_associated_record(self):
        record = Record.objects.create(
            user=self.user,
            title="Linked",
            record_type="expense_receipt",
        )
        doc = self._doc({"title": "Coffee"}, associated_record=record)
        self.assertEqual(create_record_from_ocr(doc.id), record)

    def test_returns_none_without_ocr_data(self):
        doc = self._doc(None)
        self.assertIsNone(create_record_from_ocr(doc.id))

    def test_returns_none_on_error_payload(self):
        doc = self._doc({"error": "Failed to extract details."})
        self.assertIsNone(create_record_from_ocr(doc.id))

    def test_returns_none_for_missing_document(self):
        self.assertIsNone(create_record_from_ocr(99999))

    def test_falls_back_on_invalid_type_and_currency(self):
        doc = self._doc({"title": "Coffee", "currency": "xxx", "record_type": "bogus"})
        record = create_record_from_ocr(doc.id)
        record.refresh_from_db()
        self.assertEqual(record.record_type, Record.RecordTypes.FINANCIAL_DOCUMENT)
        self.assertEqual(record.currency, DEFAULT_CURRENCY)

    def test_coerces_iso_datetime(self):
        doc = self._doc({"title": "Coffee", "transaction_date": "2024-06-15T10:30:00Z"})
        record = create_record_from_ocr(doc.id)
        record.refresh_from_db()
        self.assertEqual(record.transaction_date, date(2024, 6, 15))

    def test_defaults_title(self):
        doc = self._doc({})
        record = create_record_from_ocr(doc.id)
        self.assertEqual(record.title, "Untitled Document")


class KickoffOCRScanTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="scanner", password="pass")
        self.doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/scan.pdf",
            file_hash=_make_hash(b"scan"),
        )
        cache.clear()

    def test_kicks_off_extraction(self):
        warning = kickoff_ocr_scan(self.user, self.doc)
        self.assertIsNone(warning)
        self.assertEqual(cache.get(f"ocr_status_{self.doc.id}"), "processing")

    def test_second_kickoff_is_noop(self):
        kickoff_ocr_scan(self.user, self.doc)
        self.assertIsNone(kickoff_ocr_scan(self.user, self.doc))

    def test_returns_warning_when_scan_limit_reached(self):
        from django.utils import timezone

        from billing.models import ScanUsage

        period = timezone.now().strftime("%Y-%m")
        ScanUsage.objects.create(user=self.user, period=period, count=30)
        warning = kickoff_ocr_scan(self.user, self.doc)
        self.assertIsNotNone(warning)
        self.assertIn("limit", warning.lower())
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.status, DocumentStatus.ERROR)
        self.assertEqual(self.doc.ocr_error, "scan_limit_reached")
