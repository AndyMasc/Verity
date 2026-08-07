from decimal import Decimal

from django.contrib.auth import get_user_model

User = get_user_model()
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Notification, UserSettings
from records.models import Record


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
                "default_currency": "eur",
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
        self.assertEqual(self.user.settings.default_currency, "eur")


class ExpenseChartDataTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        UserSettings.objects.update_or_create(user=self.user, defaults={"default_currency": "usd"})

    def test_excludes_soft_deleted_records_from_totals(self):
        self.client.force_login(self.user)
        today = timezone.now().date()
        Record.objects.create(
            user=self.user,
            title="Active",
            record_type="expense_receipt",
            transaction_date=today,
            balance=Decimal("100.00"),
            currency="usd",
        )
        merged_away = Record.objects.create(
            user=self.user,
            title="Merged away",
            record_type="expense_receipt",
            transaction_date=today,
            balance=Decimal("900.00"),
            currency="usd",
        )
        merged_away.delete()

        response = self.client.get(reverse("core:expense_chart_data"), {"period": "all"})
        self.assertEqual(response.status_code, 200)
        total = sum(month["total"] for month in response.json()["months"])
        self.assertEqual(total, 100.00)

    def test_includes_active_records(self):
        self.client.force_login(self.user)
        today = timezone.now().date()
        Record.objects.create(
            user=self.user,
            title="Active",
            record_type="expense_receipt",
            transaction_date=today,
            balance=Decimal("25.50"),
            currency="usd",
        )
        response = self.client.get(reverse("core:expense_chart_data"), {"period": "all"})
        total = sum(month["total"] for month in response.json()["months"])
        self.assertEqual(total, 25.50)


class DashboardMonthlyExpensesTest(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="testuser", password="pass")
        UserSettings.objects.update_or_create(user=self.user, defaults={"default_currency": "usd"})

    def test_monthly_expenses_exclude_soft_deleted_records(self):
        self.client.force_login(self.user)
        today = timezone.now().date()
        Record.objects.create(
            user=self.user,
            title="Active",
            record_type="expense_receipt",
            transaction_date=today,
            balance=Decimal("50.00"),
            currency="usd",
        )
        merged_away = Record.objects.create(
            user=self.user,
            title="Merged away",
            record_type="expense_receipt",
            transaction_date=today,
            balance=Decimal("500.00"),
            currency="usd",
        )
        merged_away.delete()

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(float(response.context["monthly_expenses"]), 50.00)


class NotificationViewsTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.client.force_login(self.user)

    def _notification(self):
        return Notification.objects.create(
            recipient=self.user, subject="Subject", message="Message"
        )

    def test_delete_accepts_post(self):
        notification = self._notification()
        response = self.client.post(reverse("core:notification-delete", args=[notification.id]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Notification.objects.filter(pk=notification.pk).exists())

    def test_delete_requires_post(self):
        notification = self._notification()
        response = self.client.get(reverse("core:notification-delete", args=[notification.id]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(Notification.objects.filter(pk=notification.pk).exists())

    def test_mark_all_read(self):
        first = self._notification()
        second = self._notification()
        response = self.client.post(reverse("core:notification-mark-all-read"))
        self.assertRedirects(response, reverse("core:notifications"))
        self.assertFalse(
            Notification.objects.filter(pk__in=[first.pk, second.pk], is_read=False).exists()
        )
