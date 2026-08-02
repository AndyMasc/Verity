from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from django.utils import timezone

from records.models import Record


class TasksTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="taskuser", password="pass")

    def test_archive_expired_records(self):
        past = timezone.now().date() - timedelta(days=10)
        expired = Record.objects.create(
            user=self.user,
            title="Expired",
            record_type="expense_receipt",
            expiry_date=past,
            transaction_date=date(2024, 6, 15),
        )
        Record.objects.filter(pk=expired.pk).update(date_added=past - timedelta(days=1))
        from records.tasks import archive_expired_records

        archive_expired_records()
        expired.refresh_from_db()
        self.assertFalse(expired.is_active)

    def test_archive_expired_records_active_only(self):
        past = timezone.now().date() - timedelta(days=10)
        already_inactive = Record.objects.create(
            user=self.user,
            title="Already Inactive",
            record_type="expense_receipt",
            expiry_date=past,
            is_active=False,
            transaction_date=date(2024, 6, 15),
        )
        Record.objects.filter(pk=already_inactive.pk).update(date_added=past - timedelta(days=1))
        from records.tasks import archive_expired_records

        archive_expired_records()
        already_inactive.refresh_from_db()
        self.assertFalse(already_inactive.is_active)

    def test_archive_expired_records_settings_disabled(self):
        self.user.settings.auto_archive_expired_records = False
        self.user.settings.save()
        past = timezone.now().date() - timedelta(days=10)
        expired = Record.objects.create(
            user=self.user,
            title="No Auto",
            record_type="expense_receipt",
            expiry_date=past,
            transaction_date=date(2024, 6, 15),
        )
        Record.objects.filter(pk=expired.pk).update(date_added=past - timedelta(days=1))
        from records.tasks import archive_expired_records

        archive_expired_records()
        expired.refresh_from_db()
        self.assertTrue(expired.is_active)

    def test_delete_old_archived(self):
        seven_years_plus = timezone.now() - timedelta(days=365 * 7 + 1)
        old = Record.objects.create(
            user=self.user,
            title="Old Inactive",
            record_type="expense_receipt",
            is_active=False,
            expiry_date=seven_years_plus.date(),
            transaction_date=date(2015, 6, 15),
        )
        Record.objects.filter(pk=old.pk).update(
            date_added=seven_years_plus.date() - timedelta(days=80),
            last_edited=seven_years_plus,
        )
        from records.tasks import delete_7year_archived_records

        delete_7year_archived_records()
        self.assertFalse(Record.objects.filter(id=old.id).exists())

    def test_delete_old_archived_recent(self):
        recent = Record.objects.create(
            user=self.user,
            title="Recent Inactive",
            record_type="expense_receipt",
            is_active=False,
            transaction_date=date(2024, 6, 15),
        )
        from records.tasks import delete_7year_archived_records

        delete_7year_archived_records()
        self.assertTrue(Record.objects.filter(id=recent.id).exists())

    @patch("records.tasks.send_multi_channel_notification")
    def test_send_expiry_notifications(self, mock_notify):
        future = timezone.now().date() + timedelta(days=3)
        Record.objects.create(
            user=self.user,
            title="Expiring Soon",
            record_type="warranty_certificate",
            expiry_date=future,
            transaction_date=date(2024, 6, 15),
        )
        self.user.settings.enable_email_notifications = True
        self.user.settings.enable_push_notifications = True
        self.user.settings.expiring_notifications_advance_time = "7"
        self.user.settings.save()
        from records.tasks import send_expiry_notifications

        result = send_expiry_notifications()
        self.assertIsNone(result)
        mock_notify.assert_called_once()

    def test_send_expiry_notifications_no_expiring(self):
        from records.tasks import send_expiry_notifications

        result = send_expiry_notifications()
        self.assertIsNone(result)

    def test_send_expiry_notifications_user_disabled(self):
        future = timezone.now().date() + timedelta(days=3)
        Record.objects.create(
            user=self.user,
            title="Expiring Soon",
            record_type="warranty_certificate",
            expiry_date=future,
            transaction_date=date(2024, 6, 15),
        )
        self.user.settings.enable_email_notifications = False
        self.user.settings.enable_push_notifications = False
        self.user.settings.save()
        from records.tasks import send_expiry_notifications

        result = send_expiry_notifications()
        self.assertIsNone(result)
