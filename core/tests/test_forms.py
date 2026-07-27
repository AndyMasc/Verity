from django.contrib.auth.models import User
from django.test import TestCase

from core.forms import UpdateUserSettingsForm


class UpdateUserSettingsFormTest(TestCase):
    def test_form_fields(self):
        form = UpdateUserSettingsForm()
        expected = [
            "default_currency",
            "auto_archive_expired_records",
            "auto_delete_archived_records",
            "auto_delete_deleted_documents",
            "expiring_notifications_advance_time",
            "enable_push_notifications",
            "enable_email_notifications",
            "auto_create_and_organize_folders",
        ]
        self.assertEqual(list(form.fields.keys()), expected)

    def test_form_valid_data(self):
        form = UpdateUserSettingsForm(
            data={
                "default_currency": "usd",
                "auto_archive_expired_records": False,
                "auto_delete_archived_records": False,
                "enable_push_notifications": False,
                "enable_email_notifications": False,
                "expiring_notifications_advance_time": "30",
            }
        )
        self.assertTrue(form.is_valid())
