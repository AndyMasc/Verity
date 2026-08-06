"""Tests for the records service layer.

Covers archive_record, unarchive_record, bulk_toggle_archive, and
BulkLimitExceededError boundary conditions.
"""

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from records.models import AuditLog, Record
from records.services import (
    BULK_LIMIT,
    BulkLimitExceededError,
    archive_record,
    bulk_toggle_archive,
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
