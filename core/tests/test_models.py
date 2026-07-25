from django.contrib.auth.models import User
from django.test import TestCase

from core.models import UserSettings, Notification


class UserSettingsSignalTest(TestCase):
    def test_settings_created_with_user(self):
        user = User.objects.create_user(username="testuser", password="pass")
        self.assertTrue(hasattr(user, "settings"))
        self.assertIsInstance(user.settings, UserSettings)

    def test_settings_defaults(self):
        user = User.objects.create_user(username="testuser", password="pass")
        self.assertTrue(user.settings.auto_archive_expired_records)
        self.assertTrue(user.settings.auto_delete_archived_records)
        self.assertTrue(user.settings.enable_push_notifications)
        self.assertTrue(user.settings.enable_email_notifications)
        self.assertEqual(
            user.settings.expiring_notifications_advance_time,
            UserSettings.AdvanceTimeChoices.THREE_DAYS,
        )

    def test_settings_str(self):
        user = User.objects.create_user(
            username="testuser", email="test@example.com", password="pass"
        )
        self.assertEqual(str(user.settings), f"Settings for {user.email}")


class AdvanceTimeChoicesTest(TestCase):
    def test_choices_values(self):
        self.assertEqual(UserSettings.AdvanceTimeChoices.ONE_DAY, "1")
        self.assertEqual(UserSettings.AdvanceTimeChoices.THREE_DAYS, "3")
        self.assertEqual(UserSettings.AdvanceTimeChoices.ONE_WEEK, "7")
        self.assertEqual(UserSettings.AdvanceTimeChoices.ONE_MONTH, "30")


class NotificationModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_create_notification(self):
        notification = Notification.objects.create(
            recipient=self.user,
            subject="Test Subject",
            message="Test message body",
        )
        self.assertEqual(notification.recipient, self.user)
        self.assertEqual(notification.subject, "Test Subject")
        self.assertEqual(notification.message, "Test message body")
        self.assertFalse(notification.is_read)
        self.assertIsNotNone(notification.sent_at)

    def test_notification_defaults(self):
        notification = Notification.objects.create(
            recipient=self.user, subject="Test", message="Body"
        )
        self.assertFalse(notification.is_read)
