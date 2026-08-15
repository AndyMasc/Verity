import json
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from plaid_integration.models import PlaidItem
from plaid_integration.tasks import (
    choose_folder,
    _get_payment_method,
    _txn_to_record_defaults,
)
from records.models import Record, Folder


class PlaidItemModelTest(TestCase):
    """Tests for the PlaidItem model."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="test-item-123",
            access_token="access-test-456",
            institution_name="Test Bank",
            accounts_data=[
                {
                    "id": "acc1",
                    "name": "Checking",
                    "mask": "1234",
                    "type": "depository",
                    "subtype": "checking",
                },
            ],
        )

    def test_str_representation(self):
        self.assertEqual(str(self.plaid_item), "Test Bank (test-item-123)")

    def test_str_no_institution_name(self):
        item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-no-name",
            access_token="access-no-name",
        )
        self.assertEqual(str(item), "item-no-name (item-no-name)")

    def test_user_relationship(self):
        self.assertEqual(self.plaid_item.user, self.user)

    def test_item_id_unique(self):
        with self.assertRaises(Exception):
            PlaidItem.objects.create(
                user=self.user,
                item_id="test-item-123",
                access_token="access-duplicate",
            )

    def test_auto_timestamps(self):
        self.assertIsNotNone(self.plaid_item.created_at)
        self.assertIsNotNone(self.plaid_item.updated_at)

    def test_nullable_fields(self):
        item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-minimal",
            access_token="access-minimal",
        )
        self.assertEqual(item.next_cursor, "")
        self.assertEqual(item.last_error_code, "")
        self.assertEqual(item.last_error_message, "")
        self.assertIsNone(item.last_error_at)
        self.assertEqual(item.institution_name, "")
        self.assertIsNone(item.accounts_data)


class ChooseFolderTest(TestCase):
    """Tests for the choose_folder utility function."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_returns_none_for_empty_category(self):
        self.assertIsNone(choose_folder(self.user, None))
        self.assertIsNone(choose_folder(self.user, ""))
        self.assertIsNone(choose_folder(self.user, "   "))

    def test_creates_folder_when_not_exists(self):
        folder = choose_folder(self.user, "Groceries")
        self.assertIsNotNone(folder)
        self.assertEqual(folder.name, "Groceries")
        self.assertEqual(folder.user, self.user)

    def test_returns_existing_folder(self):
        existing = Folder.objects.create(user=self.user, name="Groceries")
        folder = choose_folder(self.user, "Groceries")
        self.assertEqual(folder.id, existing.id)

    def test_fuzzy_match_existing_folder(self):
        Folder.objects.create(user=self.user, name="Groceries")
        folder = choose_folder(self.user, "Food and Groceries")
        self.assertIsNotNone(folder)

    def test_folder_cache_hit(self):
        cache = {}
        folder1 = choose_folder(self.user, "Groceries", folder_cache=cache)
        folder2 = choose_folder(self.user, "Groceries", folder_cache=cache)
        self.assertEqual(folder1.id, folder2.id)
        self.assertIn("Groceries", cache)

    def test_returns_none_for_whitespace_only_words(self):
        self.assertIsNone(choose_folder(self.user, "   "))


class GetPaymentMethodTest(TestCase):
    """Tests for the _get_payment_method helper."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-1",
            access_token="access-1",
            accounts_data=[
                {"id": "acc1", "name": "Checking", "mask": "1234"},
                {"id": "acc2", "name": "Savings", "mask": "5678"},
            ],
        )

    def test_returns_formatted_payment_method(self):
        result = _get_payment_method(self.plaid_item, "acc1")
        self.assertEqual(result, "Checking (\u2022\u20221234)")

    def test_returns_name_only_when_no_mask(self):
        item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-no-mask",
            access_token="access-no-mask",
            accounts_data=[{"id": "acc1", "name": "Checking"}],
        )
        result = _get_payment_method(item, "acc1")
        self.assertEqual(result, "Checking")

    def test_returns_empty_for_no_accounts(self):
        item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-empty",
            access_token="access-empty",
            accounts_data=None,
        )
        result = _get_payment_method(item, "acc1")
        self.assertEqual(result, "")

    def test_returns_empty_for_unknown_account(self):
        result = _get_payment_method(self.plaid_item, "unknown")
        self.assertEqual(result, "")

    def test_returns_empty_for_empty_account_id(self):
        result = _get_payment_method(self.plaid_item, "")
        self.assertEqual(result, "")


class TxnToRecordDefaultsTest(TestCase):
    """Tests for the _txn_to_record_defaults helper."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.user.settings.auto_create_and_organize_folders = False
        self.user.settings.save()
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-1",
            access_token="access-1",
            accounts_data=[{"id": "acc1", "name": "Checking", "mask": "1234"}],
        )

    def test_basic_transaction_conversion(self):
        txn = {
            "name": "Amazon Purchase",
            "merchant_name": "Amazon",
            "amount": 49.99,
            "date": "2024-06-15",
            "authorized_date": "2024-06-14",
            "account_id": "acc1",
            "category": ["Shopping", "Online"],
        }
        defaults = _txn_to_record_defaults(txn, self.plaid_item)
        self.assertEqual(defaults["title"], "Amazon Purchase")
        self.assertEqual(defaults["merchant"], "Amazon")
        self.assertEqual(defaults["balance"], 49.99)
        self.assertEqual(defaults["transaction_date"], date(2024, 6, 14))
        self.assertEqual(defaults["user"], self.user)

    def test_falls_back_to_name_when_no_merchant(self):
        txn = {
            "name": "Walmart",
            "amount": 25.00,
            "date": "2024-06-15",
            "account_id": "acc1",
            "category": [],
        }
        defaults = _txn_to_record_defaults(txn, self.plaid_item)
        self.assertEqual(defaults["merchant"], "Walmart")

    def test_falls_back_to_date_when_no_authorized_date(self):
        txn = {
            "name": "Store",
            "amount": 10.00,
            "date": "2024-06-15",
            "account_id": "acc1",
        }
        defaults = _txn_to_record_defaults(txn, self.plaid_item)
        self.assertEqual(defaults["transaction_date"], date(2024, 6, 15))

    def test_auto_folder_creation_enabled(self):
        self.user.settings.auto_create_and_organize_folders = True
        self.user.settings.save()
        txn = {
            "name": "Kroger",
            "amount": 50.00,
            "date": "2024-06-15",
            "account_id": "acc1",
            "category": ["Groceries"],
        }
        defaults = _txn_to_record_defaults(txn, self.plaid_item)
        self.assertIsNotNone(defaults["folder"])

    def test_payment_method_populated(self):
        txn = {
            "name": "Store",
            "amount": 10.00,
            "date": "2024-06-15",
            "account_id": "acc1",
        }
        defaults = _txn_to_record_defaults(txn, self.plaid_item)
        self.assertIn("Checking", defaults["payment_method"])
