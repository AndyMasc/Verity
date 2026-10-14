"""Tests for reimbursements views."""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from billing.tests.helpers import give_pro_subscription
from records.models import RecordShare

from reimbursements import services
from reimbursements.models import (
    PackagePayment,
    ReimbursementPackage,
)
from reimbursements.views import validate_recipient_email

from ._helpers import _package, _record, _stripe_account, _user


class ValidateRecipientEmailTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = _user()
        self.url = reverse("reimbursements:validate-email")

    def test_valid_email(self):
        _user("valid@test.com")
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "valid@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertTrue(data["valid"])

    def test_self_send(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": self.user.email}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertFalse(data["valid"])
        self.assertIn("yourself", data["error"].lower())

    def test_external_email_is_valid(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "nobody@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertTrue(data["valid"])
        self.assertFalse(data["registered"])

    def test_registered_email_reports_user(self):
        other = _user("registered@test.com")
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "registered@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertTrue(data["valid"])
        self.assertTrue(data["registered"])
        self.assertNotIn("name", data)
        self.assertNotIn("registered_name", data)

    def test_empty_email(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": ""}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        self.assertEqual(response.status_code, 400)


class PackageListViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:package-list")

    def test_empty_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_shows_created_packages(self):
        _package(self.user, title="My Package")
        response = self.client.get(self.url)
        self.assertContains(response, "My Package")

    def test_shows_received_packages(self):
        other = _user("other@test.com")
        _package(other, self.user, title="Received Package")
        response = self.client.get(self.url)
        self.assertContains(response, "Received Package")


class PackageDetailViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("other@test.com")
        self.client.force_login(self.user)

    def test_creator_can_view(self):
        pkg = _package(self.user, self.other)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 200)

    def test_recipient_can_view(self):
        pkg = _package(self.other, self.user)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 200)

    def test_unrelated_user_cannot_view(self):
        stranger = _user("stranger@test.com")
        pkg = _package(self.user, self.other)
        self.client.force_login(stranger)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 404)


@patch("reimbursements.services.create_package_invoice")
@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class CreatePackageFromRecordsViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        _stripe_account(self.user)
        give_pro_subscription(self.user)
        self.recipient = _user("recipient@test.com")
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:create-package")

    def test_create_package(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Test Reimbursement",
                    "recipient_email": self.recipient.email,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("redirect_url", data)
        self.assertTrue(ReimbursementPackage.objects.filter(title="Test Reimbursement").exists())

    def test_unavailable_records_are_reported(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        other_record = _record(_user("someone-else@test.com"))
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id, other_record.id],
                    "title": "Partial Package",
                    "recipient_email": self.recipient.email,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pkg = ReimbursementPackage.objects.get(title="Partial Package")
        self.assertEqual(pkg.records.count(), 1)
        messages = list(getattr(response.wsgi_request, "_messages", []))
        self.assertTrue(any("1 of 2" in m.message for m in messages))

    def test_self_send_rejected(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Self",
                    "recipient_email": self.user.email,
                }
            ),
            content_type="application/json",
        )
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("yourself", data["error"].lower())

    def test_external_recipient_creates_queued_package(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "External",
                    "recipient_email": "ghost@test.com",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pkg = ReimbursementPackage.objects.get(title="External")
        self.assertEqual(pkg.status, ReimbursementPackage.Status.QUEUED)
        self.assertIsNone(pkg.recipient)
        self.assertEqual(pkg.recipient_email, "ghost@test.com")
        self.assertFalse(RecordShare.objects.filter(record=r).exists())

    def test_no_records(self, _mock_notify, _mock_rl):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [],
                    "title": "Empty",
                    "recipient_email": self.recipient.email,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_days_valid_clamped(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Clamped",
                    "recipient_email": self.recipient.email,
                    "days_valid": 9999,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pkg = ReimbursementPackage.objects.get(title="Clamped")
        max_expected = timezone.now() + timedelta(days=365)
        self.assertLess(pkg.expires_at, max_expected)

    def test_invalid_days_valid(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Bad Days",
                    "recipient_email": self.recipient.email,
                    "days_valid": "not_a_number",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class PackageDeleteViewTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.recipient = _user("recipient@test.com")
        self.pkg = _package(self.creator, recipient=self.recipient, status="open")
        self.url = reverse("reimbursements:package-delete", kwargs={"package_uuid": self.pkg.uuid})

    def test_recipient_cannot_delete_open_package_ajax_returns_403(self, _mock_rl):
        self.client.force_login(self.recipient)
        response = self.client.post(self.url, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.json())
        self.pkg.refresh_from_db()
        self.assertIsNone(self.pkg.deleted_at)

    def test_creator_can_delete_package(self, _mock_rl):
        self.client.force_login(self.creator)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.pkg.refresh_from_db()
        self.assertIsNotNone(self.pkg.deleted_at)

    def test_repeat_ajax_delete_of_deleted_package_returns_404(self, _mock_rl):
        self.client.force_login(self.creator)
        self.client.post(self.url, headers={"x-requested-with": "XMLHttpRequest"})
        response = self.client.post(self.url, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 404)

    def test_ajax_delete_failure_returns_502_and_keeps_package(self, _mock_rl):
        self.client.force_login(self.creator)
        with patch(
            "reimbursements.services.revoke_package_access",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.post(self.url, headers={"x-requested-with": "XMLHttpRequest"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("error", response.json())
        self.pkg.refresh_from_db()
        self.assertIsNone(self.pkg.deleted_at)
