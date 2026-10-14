"""Tests for record and document signals.

Covers auto-creation of UserSettings on user creation.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.models import UserSettings

User = get_user_model()


class UserSettingsSignalTest(TestCase):
    def test_settings_created_with_user(self):
        user = User.objects.create_user(username="signaluser", password="pass")
        self.assertTrue(hasattr(user, "settings"))
        self.assertIsInstance(user.settings, UserSettings)

    def test_settings_defaults(self):
        user = User.objects.create_user(username="signaldefaults", password="pass")
        self.assertTrue(user.settings.auto_archive_expired_records)
        self.assertTrue(user.settings.auto_delete_archived_records)
        self.assertTrue(user.settings.enable_push_notifications)
        self.assertTrue(user.settings.enable_email_notifications)

    def test_settings_str(self):
        user = User.objects.create_user(
            username="signalstr", email="test@example.com", password="pass"
        )
        self.assertEqual(str(user.settings), "Settings for test@example.com")

    def test_settings_not_overwritten_on_second_save(self):
        user = User.objects.create_user(username="signaldouble", password="pass")
        user.settings.auto_archive_expired_records = False
        user.settings.save()
        user.save()
        user.settings.refresh_from_db()
        self.assertFalse(user.settings.auto_archive_expired_records)
