from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


class LandingPageTest(TestCase):
    def test_status(self):
        response = self.client.get(reverse("core:landing_page"))
        self.assertEqual(response.status_code, 200)

    def test_template(self):
        response = self.client.get(reverse("core:landing_page"))
        self.assertTemplateUsed(response, "core/landing_page.html")


class PrivacyPolicyTest(TestCase):
    def test_status(self):
        response = self.client.get(reverse("core:privacy_policy"))
        self.assertEqual(response.status_code, 200)

    def test_template(self):
        response = self.client.get(reverse("core:privacy_policy"))
        self.assertTemplateUsed(response, "core/privacy_policy.html")


class HealthCheckTest(TestCase):
    def test_health_check_returns_200(self):
        response = self.client.get(reverse("core:health_check"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "healthy")
        self.assertEqual(data["database"]["status"], "connected")


class DashboardViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_login_required(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/dashboard.html")

    def test_context_has_counts(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:dashboard"))
        self.assertIn("merged_records_count", response.context)
        self.assertIn("pending_ocr_count", response.context)
        self.assertIn("records", response.context)
        self.assertIn("expiring_soon", response.context)
        self.assertIn("orphaned_document_count", response.context)


class ProfilePageViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")

    def test_login_required(self):
        response = self.client.get(reverse("core:profile_page"))
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:profile_page"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/profile_page.html")

    def test_context_has_settings(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:profile_page"))
        self.assertIn("user_settings", response.context)
        self.assertEqual(response.context["user_settings"].user, self.user)

    def test_post_updates_settings(self):
        self.client.force_login(self.user)
        self.user.settings.refresh_from_db()
        response = self.client.post(
            reverse("core:profile_page"),
            {
                "auto_archive_expired_records": False,
                "auto_delete_archived_records": False,
                "enable_push_notifications": False,
                "enable_email_notifications": False,
                "expiring_notifications_advance_time": "7",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        self.user.settings.refresh_from_db()
        self.assertFalse(self.user.settings.auto_archive_expired_records)
        self.assertFalse(self.user.settings.enable_email_notifications)
        self.assertEqual(self.user.settings.expiring_notifications_advance_time, "7")
