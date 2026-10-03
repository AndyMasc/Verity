from datetime import date, timedelta

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from django.utils import timezone

from records.filters import RecordFilter
from records.models import Record, Folder

from ._helpers import make_filter_request


class RecordFilterTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="filteruser", password="pass")
        self.active = Record.objects.create(
            user=self.user,
            title="Active Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.inactive = Record.objects.create(
            user=self.user,
            title="Inactive Record",
            record_type="voucher",
            is_active=False,
            transaction_date=date(2024, 6, 15),
        )
        self.with_expiry = Record.objects.create(
            user=self.user,
            title="Has Expiry",
            record_type="warranty_certificate",
            expiry_date=timezone.now().date() + timedelta(days=3),
            transaction_date=date(2024, 6, 15),
        )

    def test_filter_no_params(self):
        f = RecordFilter(
            {},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        self.assertEqual(f.qs.count(), 3)

    def test_filter_is_active_true(self):
        f = RecordFilter(
            {"is_active": "true"},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        self.assertIn(self.active, f.qs)
        self.assertNotIn(self.inactive, f.qs)

    def test_filter_is_active_false(self):
        f = RecordFilter(
            {"is_active": "false"},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        self.assertIn(self.inactive, f.qs)
        self.assertNotIn(self.active, f.qs)

    def test_filter_expiring_soon(self):
        f = RecordFilter(
            {"expiring_soon": True},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        self.assertIn(self.with_expiry, f.qs)

    def test_filter_record_type(self):
        Record.objects.create(
            user=self.user,
            title="Active Voucher",
            record_type="voucher",
            transaction_date=date(2024, 6, 15),
        )
        f = RecordFilter(
            {"record_type": "voucher"},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        count = f.qs.count()
        self.assertGreaterEqual(count, 1)

    def test_filter_folder_direct_method(self):
        folder = Folder.objects.create(user=self.user, name="Folder 1")
        self.active.folder = folder
        self.active.save()
        f = RecordFilter(
            {"folder": str(folder.id)},
            queryset=Record.objects.for_user(self.user),
            request=make_filter_request(self.user),
        )
        result = f.filter_by_folder(Record.objects.for_user(self.user), "folder", str(folder.id))
        self.assertIn(self.active, result)
        self.assertNotIn(self.inactive, result)
