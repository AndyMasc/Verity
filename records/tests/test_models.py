from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from django.utils import timezone

from records.models import Record, Folder


class RecordModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )

    def test_str(self):
        self.assertEqual(str(self.record), "Test Record")

    def test_badge_classes_by_record_type(self):
        classes = self.record.badge_classes
        self.assertIn("bg-emerald", classes)
        self.assertIn("text-emerald", classes)

    def test_badge_classes_other_type(self):
        self.record.record_type = "voucher"
        self.record.save()
        classes = self.record.badge_classes
        self.assertIn("bg-amber", classes)

    def test_is_expired(self):
        self.record.expiry_date = timezone.now().date() - timedelta(days=1)
        self.record.save()
        self.assertTrue(self.record.is_expired)

    def test_is_not_expired_no_expiry(self):
        self.assertFalse(self.record.is_expired)

    def test_is_not_expired_future(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=1)
        self.record.save()
        self.assertFalse(self.record.is_expired)

    def test_is_expiring_soon_30_days(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=29)
        self.record.save()
        self.assertTrue(self.record.is_expiring_soon())

    def test_is_not_expiring_soon_beyond_30(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=31)
        self.record.save()
        self.assertFalse(self.record.is_expiring_soon())

    def test_is_not_expiring_soon_no_expiry(self):
        self.assertFalse(self.record.is_expiring_soon())

    def test_date_added_auto_now(self):
        self.assertIsNotNone(self.record.date_added)
        self.assertIsNotNone(self.record.last_edited)

    def test_is_active_default(self):
        self.assertTrue(self.record.is_active)

    def test_ordering_by_last_edited_desc(self):
        r1 = Record.objects.create(user=self.user, title="First", record_type="expense_receipt")
        r2 = Record.objects.create(user=self.user, title="Second", record_type="voucher")
        qs = Record.objects.all()
        self.assertEqual(qs.first(), r2)

    def test_default_balance(self):
        self.assertIsNone(self.record.balance)

    def test_default_strings(self):
        self.assertEqual(self.record.merchant, "")
        self.assertEqual(self.record.products, "")
        self.assertEqual(self.record.notes, "")

    def test_default_record_type(self):
        record = Record.objects.create(user=self.user, title="Default Type")
        self.assertEqual(record.record_type, "expense_receipt")

    def test_queryset_for_user(self):
        user2 = User.objects.create_user(username="user2", password="pass")
        Record.objects.create(user=user2, title="Other", record_type="expense_receipt")
        self.assertEqual(Record.objects.for_user(self.user).count(), 1)
        self.assertEqual(Record.objects.for_user(user2).count(), 1)

    def test_queryset_active(self):
        Record.objects.create(
            user=self.user,
            title="Inactive",
            record_type="voucher",
            is_active=False,
        )
        qs = Record.objects.active()
        self.assertEqual(qs.count(), 1)

    def test_queryset_archived_inactive(self):
        inactive = Record.objects.create(
            user=self.user,
            title="Inactive",
            record_type="voucher",
            is_active=False,
        )
        qs = Record.objects.archived()
        self.assertIn(inactive, qs)
        self.assertNotIn(self.record, qs)

    def test_queryset_with_documents(self):
        import hashlib
        from documents.models import DocumentData

        DocumentData.objects.create(
            user=self.user,
            associated_record=self.record,
            filepath="users/1/doc.pdf",
            file_hash=hashlib.sha256(b"doc").hexdigest(),
        )
        qs = Record.objects.with_documents()
        self.assertIn(self.record, qs)

    def test_queryset_expiring_soon(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=7)
        self.record.save()
        qs = Record.objects.expiring_soon()
        self.assertIn(self.record, qs)

    def test_queryset_expiring_soon_default_30_days(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=25)
        self.record.save()
        qs = Record.objects.expiring_soon()
        self.assertIn(self.record, qs)

    def test_queryset_expiring_soon_excludes_beyond_30(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=35)
        self.record.save()
        qs = Record.objects.expiring_soon()
        self.assertNotIn(self.record, qs)

    def test_queryset_expired(self):
        self.record.expiry_date = timezone.now().date() - timedelta(days=1)
        self.record.save()
        qs = Record.objects.expired()
        self.assertIn(self.record, qs)

    def test_queryset_expired_excludes_future(self):
        self.record.expiry_date = timezone.now().date() + timedelta(days=1)
        self.record.save()
        qs = Record.objects.expired()
        self.assertNotIn(self.record, qs)

    def test_expired_excludes_inactive(self):
        self.record.expiry_date = timezone.now().date() - timedelta(days=1)
        self.record.is_active = False
        self.record.save()
        qs = Record.objects.expired()
        self.assertNotIn(self.record, qs)

    def test_smart_search_title(self):
        Record.objects.create(
            user=self.user,
            title="Tax Return 2024",
            record_type="tax_document",
        )
        qs = Record.objects.smart_search("Tax Return")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_merchant(self):
        Record.objects.create(
            user=self.user,
            title="Purchase",
            merchant="Amazon",
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("Amazon")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_products(self):
        Record.objects.create(
            user=self.user,
            title="Grocery",
            products="Milk|Eggs|Bread",
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("Eggs")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_notes(self):
        Record.objects.create(
            user=self.user,
            title="Note",
            notes="Important document",
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("Important")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_record_type(self):
        Record.objects.create(
            user=self.user,
            title="Warranty Doc",
            record_type="warranty_certificate",
        )
        qs = Record.objects.smart_search("Warranty")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_balance(self):
        Record.objects.create(
            user=self.user,
            title="Expense",
            balance=Decimal("150.00"),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("150")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_balance_not_matching(self):
        Record.objects.create(
            user=self.user,
            title="Expense",
            balance=Decimal("200.00"),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("150")
        self.assertEqual(qs.count(), 0)

    def test_smart_search_isodate(self):
        Record.objects.create(
            user=self.user,
            title="Dated",
            transaction_date=date(2024, 6, 15),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("2024-06-15")
        self.assertEqual(qs.count(), 2)

    def test_smart_search_year(self):
        Record.objects.create(
            user=self.user,
            title="This Year",
            transaction_date=date(2024, 6, 15),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("2024")
        self.assertEqual(qs.count(), 2)

    def test_smart_search_with_setup_record_isolation(self):
        if self.record.title == "Test Record":
            self.assertEqual(Record.objects.count(), 1)

    def test_smart_search_month_name(self):
        today = timezone.now().date()
        Record.objects.create(
            user=self.user,
            title="June Purchase",
            transaction_date=date(today.year, 6, 15),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("june")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_relative_today(self):
        Record.objects.create(
            user=self.user,
            title="Today",
            transaction_date=timezone.now().date(),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("today")
        self.assertGreaterEqual(qs.count(), 1)

    def test_smart_search_relative_yesterday(self):
        Record.objects.create(
            user=self.user,
            title="Yesterday",
            transaction_date=timezone.now().date() - timedelta(days=1),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("yesterday")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_relative_tomorrow(self):
        Record.objects.create(
            user=self.user,
            title="Tomorrow Record",
            transaction_date=timezone.now().date() + timedelta(days=1),
            record_type="expense_receipt",
        )
        qs = Record.objects.smart_search("tomorrow")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_empty_returns_all(self):
        qs = Record.objects.smart_search("")
        self.assertEqual(qs.count(), 1)

    def test_smart_search_no_match(self):
        qs = Record.objects.smart_search("zzzznotfound")
        self.assertEqual(qs.count(), 0)

    def test_smart_search_year_trumps_month(self):
        today = timezone.now().date()
        self.record.transaction_date = date(today.year, 3, 15)
        self.record.save()
        qs = Record.objects.smart_search(str(today.year))
        self.assertEqual(qs.count(), 1)


class RecordModelExpiryNotificationTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_expiry_notification_sent_default(self):
        record = Record.objects.create(user=self.user, title="Test", record_type="expense_receipt")
        self.assertFalse(record.expiry_notification_sent)


class RecordModelPlaidTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="plaidtest", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )

    def test_is_plaid_record_true(self):
        record = Record.objects.create(
            user=self.user,
            title="Plaid Record",
            record_type="expense_receipt",
            plaid_transaction_id="txn_123",
            transaction_date=date(2024, 6, 15),
        )
        self.assertTrue(record.is_plaid_record)

    def test_is_plaid_record_false(self):
        self.assertFalse(self.record.is_plaid_record)

    def test_nickname_default(self):
        self.assertEqual(self.record.nickname, "")

    def test_save_allows_payment_method_for_plaid_record(self):
        record = Record.objects.create(
            user=self.user,
            title="Plaid Record",
            record_type="expense_receipt",
            plaid_transaction_id="txn_123",
            payment_method="Chase (••1234)",
            transaction_date=date(2024, 6, 15),
        )
        record.payment_method = "Modified"
        record.save(update_fields=["payment_method"])
        record.refresh_from_db()
        self.assertEqual(record.payment_method, "Modified")

    def test_save_allows_payment_method_for_non_plaid_record(self):
        self.record.payment_method = "Visa (1234)"
        self.record.save(update_fields=["payment_method"])
        self.record.refresh_from_db()
        self.assertEqual(self.record.payment_method, "Visa (1234)")


class FolderModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="folderuser", password="pass")

    def test_create_folder(self):
        folder = Folder.objects.create(user=self.user, name="Tax Documents")
        self.assertEqual(str(folder), "Tax Documents")
        self.assertIsNotNone(folder.created_at)

    def test_folder_default_no_color_field(self):
        folder = Folder.objects.create(user=self.user, name="Default")
        self.assertFalse(hasattr(folder, "color"))
