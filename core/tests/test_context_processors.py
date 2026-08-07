import json
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from core.context_processors import webpush_status
from core.models import Notification


class WebpushContextProcessorTest(TestCase):
    def test_unauthenticated(self):
        class MockUser:
            is_authenticated = False
            webpush_info = type("Mgr", (), {"count": lambda self: 0})()

        request = type("Request", (), {"user": MockUser()})()
        result = webpush_status(request)
        self.assertFalse(result["webpush_enabled"])
        self.assertEqual(result["webpush_subscription_count"], 0)


class NotificationsServiceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_build_site_context(self):
        from core.services.notifications import build_site_context

        context = build_site_context()
        self.assertIn("site_url", context)
        self.assertIn("site_domain", context)
        self.assertIn("current_site", context)
        self.assertIn("domain", context["current_site"])
        self.assertIn("name", context["current_site"])

    def test_build_site_context_without_site(self):
        from core.services.notifications import build_site_context
        from django.contrib.sites.models import Site

        Site.objects.all().delete()
        context = build_site_context()
        self.assertIn("site_url", context)
        self.assertEqual(context["current_site"]["name"], "Papertrail")

    def test_build_expiry_webpush_payload(self):
        from core.services.notifications import build_expiry_webpush_payload

        payload = build_expiry_webpush_payload(3)
        self.assertEqual(payload["head"], "Record Expiry Alert")
        self.assertEqual(payload["body"], "You have 3 records expiring soon.")

    def test_build_expiry_webpush_payload_singular(self):
        from core.services.notifications import build_expiry_webpush_payload

        payload = build_expiry_webpush_payload(1)
        self.assertEqual(payload["body"], "You have 1 record expiring soon.")

    def test_user_can_receive_email_default_true(self):
        from core.services.notifications import _user_can_receive_email

        user_no_settings = User.objects.create_user(username="nosettings", password="pass")
        result = _user_can_receive_email(user_no_settings)
        self.assertTrue(result)

    def test_user_can_receive_email_true(self):
        from core.services.notifications import _user_can_receive_email

        self.user.settings.enable_email_notifications = True
        self.user.settings.save()
        result = _user_can_receive_email(self.user)
        self.assertTrue(result)

    def test_user_can_receive_email_false(self):
        from core.services.notifications import _user_can_receive_email

        self.user.settings.enable_email_notifications = False
        self.user.settings.save()
        result = _user_can_receive_email(self.user)
        self.assertFalse(result)

    def test_user_can_receive_push_no_settings(self):
        from core.services.notifications import _user_can_receive_push

        user_no_settings = User.objects.create_user(username="nosettings2", password="pass")
        result = _user_can_receive_push(user_no_settings)
        self.assertFalse(result)

    def test_user_can_receive_push_disabled(self):
        from core.services.notifications import _user_can_receive_push

        self.user.settings.enable_push_notifications = False
        self.user.settings.save()
        result = _user_can_receive_push(self.user)
        self.assertFalse(result)

    def test_build_expiry_email_context(self):
        from core.services.notifications import build_expiry_email_context

        context = build_expiry_email_context(
            user=self.user,
            records=[],
            remaining_count=0,
            total_records_count=0,
            auto_archive_msg="",
            action_url="https://example.com",
        )
        self.assertIn("user", context)
        self.assertIn("records", context)
        self.assertIn("action_url", context)
        self.assertIn("site_url", context)

    @patch("core.services.notifications.fire_single_webpush")
    @patch("core.services.notifications.send_email_notification")
    @patch("core.services.notifications._user_can_receive_push", return_value=True)
    def test_send_multi_channel_both(self, mock_can_push, mock_email, mock_push):
        from core.services.notifications import send_multi_channel_notification

        self.user.settings.enable_push_notifications = True
        self.user.settings.enable_email_notifications = True
        self.user.settings.save()
        send_multi_channel_notification(
            user=self.user,
            subject="Test",
            text_body="Text",
            html_body="<p>HTML</p>",
            webpush_payload={"head": "Test"},
            send_push=True,
            send_email=True,
        )
        mock_push.delay.assert_called_once()
        mock_email.assert_called_once()

    @patch("core.services.notifications.send_email_notification")
    def test_send_multi_channel_db_only(self, mock_email):
        from core.services.notifications import send_multi_channel_notification

        send_multi_channel_notification(
            user=self.user,
            subject="Test",
            text_body="Text",
            html_body="<p>HTML</p>",
            send_push=False,
            send_email=False,
            send_db=True,
            db_message="DB Test",
        )
        mock_email.assert_not_called()
        self.assertTrue(Notification.objects.filter(message="DB Test").exists())
