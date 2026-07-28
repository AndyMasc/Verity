from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from records.forms import AddRecordForm, RecordUpdateForm, FolderForm
from records.models import Record, Folder


class FolderFormTest(TestCase):
    def test_valid_data(self):
        form = FolderForm(data={"name": "New Folder"})
        self.assertTrue(form.is_valid())

    def test_blank_name(self):
        form = FolderForm(data={"name": ""})
        self.assertFalse(form.is_valid())

    def test_max_length(self):
        form = FolderForm(data={"name": "A" * 256})
        self.assertFalse(form.is_valid())

    def test_no_color_field(self):
        form = FolderForm()
        self.assertNotIn("color", form.fields)


class BaseRecordFormTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="formuser", password="pass")

    def test_required_fields(self):
        form = AddRecordForm(user=self.user, data={})
        self.assertFalse(form.is_valid())
        self.assertIn("title", form.errors)
        self.assertIn("record_type", form.errors)
        self.assertIn("transaction_date", form.errors)
        self.assertIn("merchant", form.errors)
        self.assertIn("balance", form.errors)

    def test_minimal_valid(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())

    def test_balance_negative(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "balance": "-10.00",
                "currency": "usd",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertFalse(form.is_valid())

    def test_balance_valid(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "balance": "150.50",
                "currency": "usd",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["balance"], Decimal("150.50"))

    def test_transaction_date_future(self):
        future = (timezone.now().date() + timedelta(days=5)).isoformat()
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": future,
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertFalse(form.is_valid())

    def test_transaction_date_past_valid(self):
        past = (timezone.now().date() - timedelta(days=5)).isoformat()
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": past,
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())

    def test_expiry_before_transaction(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "expiry_date": "2024-06-14",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertFalse(form.is_valid())

    def test_expiry_equal_to_transaction(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "expiry_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())

    def test_expiry_after_transaction(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "expiry_date": "2024-06-20",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())

    def test_expiry_without_transaction_date(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "expiry_date": "2024-06-20",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertFalse(form.is_valid())

    def test_balance_none(self):
        form = AddRecordForm(
            user=self.user,
            data={
                "title": "Test",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "0.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertTrue(form.is_valid())

    def test_folder_queryset_filtered_by_user(self):
        folder = Folder.objects.create(user=self.user, name="My Folder")
        form = AddRecordForm(user=self.user)
        self.assertIn(folder, form.fields["folder"].queryset)


class AddRecordFormTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="formuser", password="pass")

    def test_form_fields(self):
        form = AddRecordForm(user=self.user)
        expected = [
            "title",
            "products",
            "merchant",
            "balance",
            "currency",
            "transaction_date",
            "expiry_date",
            "record_type",
            "notes",
            "payment_method",
            "nickname",
            "folder",
        ]
        self.assertEqual(list(form.fields.keys()), expected)


class RecordUpdateFormTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="formuser", password="pass")

    def test_fields_match(self):
        update_fields = set(RecordUpdateForm().fields.keys())
        add_fields = set(AddRecordForm(user=self.user).fields.keys())
        self.assertEqual(update_fields, add_fields)
