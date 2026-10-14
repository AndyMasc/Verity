"""Expanded edge-case tests for Record and Folder models.

Covers smart_search, with_record_counts, RecordQuerySet.delete guard,
FolderQuerySet, and additional model properties.
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from records.models import Folder, Record

User = get_user_model()


class RecordQuerySetDeleteGuardTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="guarduser", password="pass")

    def test_bulk_delete_raises_typeerror(self):
        qs = Record.objects.filter(user=self.user)
        with self.assertRaises(TypeError):
            qs.delete()

    def test_allow_bulk_delete_permits(self):
        Record.objects.create(
            user=self.user,
            title="Del",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        qs = Record.objects.filter(user=self.user).allow_bulk_delete()
        count, _ = qs.delete()
        self.assertEqual(count, 1)


class RecordSmartSearchTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="searchuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Starbucks Coffee",
            merchant="Starbucks",
            products="Latte",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )

    def test_search_by_title(self):
        qs = Record.objects.for_user(self.user).smart_search("Starbucks")
        self.assertIn(self.record, qs)

    def test_search_by_products(self):
        qs = Record.objects.for_user(self.user).smart_search("Latte")
        self.assertIn(self.record, qs)

    def test_search_no_match(self):
        qs = Record.objects.for_user(self.user).smart_search("NonExistentXYZ")
        self.assertNotIn(self.record, qs)


class FolderQuerySetTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="foldertest", password="pass")

    def test_with_record_counts(self):
        folder = Folder.objects.create(user=self.user, name="Test")
        Record.objects.create(
            user=self.user,
            title="In Folder",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
            folder=folder,
        )
        Record.objects.create(
            user=self.user,
            title="Not In Folder",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        qs = Folder.objects.filter(user=self.user).with_record_counts()
        folder_data = qs.get(id=folder.id)
        self.assertEqual(folder_data.active_records_count, 1)


class RecordModelEdgeCasesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="edgeuser", password="pass")

    def test_is_expiring_soon_boundary_30(self):
        record = Record.objects.create(
            user=self.user,
            title="Boundary",
            record_type="warranty_certificate",
            transaction_date=date(2024, 1, 1),
        )
        record.expiry_date = timezone.now().date() + timedelta(days=30)
        record.save()
        self.assertTrue(record.is_expiring_soon())

    def test_is_expired_today(self):
        record = Record.objects.create(
            user=self.user,
            title="Today",
            record_type="warranty_certificate",
            transaction_date=date(2024, 1, 1),
        )
        record.expiry_date = timezone.now().date()
        record.save()
        self.assertFalse(record.is_expired)

    def test_badge_classes_expense_receipt(self):
        record = Record.objects.create(
            user=self.user,
            title="Receipt",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        classes = record.badge_classes
        self.assertIn("bg-emerald", classes)

    def test_badge_classes_voucher(self):
        record = Record.objects.create(
            user=self.user,
            title="Voucher",
            record_type="voucher",
            transaction_date=date(2024, 6, 15),
        )
        classes = record.badge_classes
        self.assertIn("bg-amber", classes)

    def test_str_returns_title(self):
        record = Record.objects.create(
            user=self.user,
            title="My Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.assertEqual(str(record), "My Record")

    def test_soft_delete_sets_inactive(self):
        record = Record.objects.create(
            user=self.user,
            title="To Soft Delete",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        record.delete()
        record.refresh_from_db()
        self.assertFalse(record.is_active)

    def test_hard_delete_removes_record(self):
        record = Record.objects.create(
            user=self.user,
            title="To Hard Delete",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        pk = record.pk
        record.hard_delete()
        self.assertFalse(Record.objects.filter(pk=pk).exists())
