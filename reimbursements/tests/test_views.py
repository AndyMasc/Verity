"""Tests for reimbursements views."""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from billing.tests.helpers import give_pro_subscription
from records.models import RecordShare

from reimbursements import services
from reimbursements.models import (
    PackageEmailVerification,
    PackagePayment,
    ReimbursementPackage,
)
from reimbursements.verification import send_verification_code
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


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
@patch("reimbursements.notifications.send_package_created_notification")
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
class CreatePackageCheckoutViewTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        _stripe_account(self.creator)
        self.payer = _user("payer@test.com")
        self.pkg = _package(self.creator, self.payer, title="Checkout Test")
        self.client.force_login(self.payer)
        self.url = reverse("reimbursements:create-checkout", kwargs={"package_uuid": self.pkg.uuid})

    def test_creator_cannot_pay(self, _mock_rl):
        self.client.force_login(self.creator)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_paid_package_redirects(self, _mock_rl):
        self.pkg.mark_as_paid(self.payer)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_expired_package_redirects(self, _mock_rl):
        self.pkg.expires_at = timezone.now() - timedelta(days=1)
        self.pkg.save(update_fields=["expires_at"])
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_not_allowed(self, _mock_rl):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class PaymentSuccessViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("other@test.com")
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:payment-success")

    def test_no_package_param(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_valid_package(self):
        pkg = _package(self.user, self.other)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)

    def test_payer_can_view_package_before_webhook(self):
        pkg = _package(self.user, self.other)
        PackagePayment.objects.create(
            package=pkg,
            payer=self.other,
            stripe_checkout_session_id="cs_test_foo",
            amount_paid=pkg.total_amount,
        )
        self.client.force_login(self.other)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, pkg.title)

    def test_other_users_package_not_visible(self):
        stranger = _user("stranger@test.com")
        pkg = _package(self.user, self.other)
        self.client.force_login(stranger)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, pkg.title)


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class PublicPayFlowTest(TestCase):
    """External recipients verify their email before viewing or paying."""

    def setUp(self):
        self.creator = _user("creator@test.com")
        _stripe_account(self.creator)
        self.record = _record(self.creator, Decimal("25.00"))
        self.pkg, _ = services.create_reimbursement_package(
            creator=self.creator,
            recipient_email="external@test.com",
            record_ids=[self.record.pk],
            title="External Request",
            days_valid=7,
        )
        self.pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": self.pkg.uuid})
        self.request_code_url = reverse(
            "reimbursements:pay-request-code", kwargs={"package_uuid": self.pkg.uuid}
        )
        self.verify_code_url = reverse(
            "reimbursements:pay-verify-code", kwargs={"package_uuid": self.pkg.uuid}
        )
        self.checkout_url = reverse(
            "reimbursements:pay-checkout", kwargs={"package_uuid": self.pkg.uuid}
        )

    def _post(self, url, data):
        return self.client.post(url, data)

    def test_unverified_visitor_only_sees_verification_step(self, _mock_rl):
        resp = self.client.get(self.pay_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Verify identity")
        self.assertNotContains(resp, "External Request")
        self.assertNotContains(resp, "Total Due")

    def test_code_step_renders_with_email(self, _mock_rl):
        resp = self.client.get(self.pay_url, {"step": "code", "email": "external@test.com"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "external@test.com")

    @patch("reimbursements.verification.send_background_email")
    def test_request_code_redirects_carries_email(self, _mock_email, _mock_rl):
        resp = self._post(self.request_code_url, {"email": "external@test.com"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("step=code", resp.url)
        self.assertIn("email=external%40test.com", resp.url)

    @patch("reimbursements.verification.send_background_email")
    def test_verify_code_failure_keeps_email(self, _mock_email, _mock_rl):
        send_verification_code(self.pkg, "external@test.com")
        resp = self._post(self.verify_code_url, {"email": "external@test.com", "code": "wrongcode"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("email=external%40test.com", resp.url)

    @patch("reimbursements.verification.send_background_email")
    @patch("reimbursements.verification._generate_code", return_value="123456")
    def test_verify_flow_unlocks_package(self, _mock_gen, mock_email, _mock_rl):
        resp = self._post(self.request_code_url, {"email": "someone@else.com"})
        self.assertEqual(resp.status_code, 302)
        mock_email.send.assert_not_called()

        resp = self._post(self.request_code_url, {"email": "external@test.com"})
        self.assertEqual(resp.status_code, 302)
        mock_email.send.assert_called_once()
        verification = PackageEmailVerification.objects.get(package=self.pkg)
        self.assertIsNone(verification.verified_at)

        resp = self._post(self.verify_code_url, {"email": "external@test.com", "code": "000000"})
        self.assertEqual(resp.status_code, 302)
        verification.refresh_from_db()
        self.assertEqual(verification.attempts, 1)
        self.assertNotContains(self.client.get(self.pay_url), "External Request")

        resp = self._post(self.verify_code_url, {"email": "external@test.com", "code": "123456"})
        self.assertEqual(resp.status_code, 302)
        with patch("reimbursements.models.get_rates", return_value={}):
            page = self.client.get(self.pay_url)
        self.assertContains(page, "External Request")
        self.assertContains(page, "Total Due")
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.status, ReimbursementPackage.Status.OPEN)

    def test_expired_code_rejected(self, _mock_rl):
        send_verification_code(self.pkg, "external@test.com")
        verification = PackageEmailVerification.objects.get(package=self.pkg)
        verification.expires_at = timezone.now() - timedelta(minutes=1)
        verification.save(update_fields=["expires_at"])
        resp = self._post(
            self.verify_code_url,
            {"email": "external@test.com", "code": "wrongcode"},
        )
        self.assertEqual(resp.status_code, 302)
        verification.refresh_from_db()
        self.assertIsNone(verification.verified_at)

    def test_checkout_without_verification_redirects(self, _mock_rl):
        with patch("reimbursements.services.create_checkout_session") as mock_session:
            resp = self._post(self.checkout_url, {})
        mock_session.assert_not_called()
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.url.startswith(self.pay_url))

    @patch("reimbursements.services.create_checkout_session")
    def test_verified_checkout_creates_external_payment(self, mock_session, _mock_rl):
        session = self.client.session
        session[f"_reimbursement_verified:{self.pkg.uuid}"] = True
        session.save()

        mock_session.return_value = type(
            "S", (), {"id": "cs_new", "url": "https://checkout.stripe.com/x"}
        )()
        resp = self._post(self.checkout_url, {})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "https://checkout.stripe.com/x")
        mock_session.assert_called_once()
        payment = PackagePayment.objects.get(package=self.pkg)
        self.assertIsNone(payment.payer)
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.status, ReimbursementPackage.Status.OPEN)

    def test_paid_package_shows_already_paid(self, _mock_rl):
        self.pkg.status = ReimbursementPackage.Status.PAID
        self.pkg.save(update_fields=["status"])
        resp = self.client.get(self.pay_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Request already paid")
        self.assertNotContains(resp, "External Request")
