import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import stripe
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from billing.tests.helpers import give_pro_subscription
from records.models import AuditLog, Record, RecordShare
from reimbursements import services
from reimbursements.models import (
    PackageEmailVerification,
    PackagePayment,
    ReimbursementPackage,
    StripeAccount,
)
from reimbursements.verification import send_verification_code
from reimbursements.views import validate_recipient_email
from reimbursements.webhooks import apply_paid_session, process_stripe_event

User = get_user_model()


def _user(email="test@example.com", **kwargs):
    username = kwargs.pop("username", email.split("@")[0])
    return User.objects.create_user(
        username=username, email=email, password="testpass123", **kwargs
    )


def _package(creator, recipient=None, status="open", **kwargs):
    return ReimbursementPackage.objects.create(
        creator=creator,
        recipient=recipient,
        title=kwargs.get("title", "Test Package"),
        status=status,
        **{k: v for k, v in kwargs.items() if k not in ("title", "status")},
    )


def _record(user, balance=Decimal("25.00")):
    return Record.objects.create(
        user=user,
        title="Test Expense",
        balance=balance,
        record_type="expense",
        is_active=True,
    )


def _reconcile_session(session_id, *, bad, good):
    """Fake retrieve_checkout_session for reconciliation tests.

    Failures and successes are routed by session id rather than call order,
    so the test does not depend on queryset iteration order.
    """
    if session_id == bad:
        raise stripe.error.StripeError("boom")
    return _FakeSession(
        id=session_id,
        payment_status="paid",
        amount_total=5000,
        currency="usd",
    )


def _stripe_account(user, active=True):
    StripeAccount.objects.filter(user=user).update(
        stripe_account_id="acct_test123" if active else None,
        stripe_details_submitted=active,
        charges_enabled=active,
        payouts_enabled=active,
    )
    user.stripe_account.refresh_from_db()
    return user.stripe_account


class ReimbursementPackageModelTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.recipient = _user("recipient@test.com")

    def test_str(self):
        pkg = _package(self.creator)
        self.assertIn(pkg.title, str(pkg))

    def test_total_amount(self):
        pkg = _package(self.creator)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.50"))
        pkg.records.add(r1, r2)
        self.assertEqual(pkg.total_amount, Decimal("30.50"))

    def test_total_amount_excludes_inactive(self):
        pkg = _package(self.creator)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.00"))
        r2.is_active = False
        r2.save()
        pkg.records.add(r1, r2)
        self.assertEqual(pkg.total_amount, Decimal("10.00"))

    def test_total_amount_cents(self):
        pkg = _package(self.creator)
        r = _record(self.creator, Decimal("15.75"))
        pkg.records.add(r)
        self.assertEqual(pkg.total_amount_cents, 1575)

    def test_mark_as_paid(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(pkg.paid_by, self.recipient)
        self.assertIsNotNone(pkg.paid_at)

    def test_mark_as_paid_idempotent(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        first_paid_at = pkg.paid_at
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.paid_at, first_paid_at)

    def test_mark_as_refunded(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        pkg.mark_as_refunded()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)
        self.assertIsNone(pkg.paid_by)
        self.assertIsNone(pkg.paid_at)

    def test_mark_as_refunded_idempotent(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_refunded()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_is_expired_false(self):
        pkg = _package(self.creator, expires_at=timezone.now() + timedelta(days=7))
        self.assertFalse(pkg.is_expired)

    def test_is_expired_true(self):
        pkg = _package(self.creator, expires_at=timezone.now() - timedelta(days=1))
        self.assertTrue(pkg.is_expired)

    def test_is_expired_none(self):
        pkg = _package(self.creator, expires_at=None)
        self.assertFalse(pkg.is_expired)

    def test_mark_as_paid_without_payer_skips_record(self):
        pkg = _package(self.creator, recipient=None, recipient_email="ext@test.com")
        r = _record(self.creator, Decimal("25.00"))
        pkg.records.add(r)
        pkg.mark_as_paid(None)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertIsNone(pkg.paid_by)
        self.assertIsNotNone(pkg.paid_at)
        pkg.records.get().refresh_from_db()
        self.assertTrue(pkg.records.get().reimbursed)
        self.assertFalse(
            Record.objects.filter(
                title=f"Reimbursement: {pkg.title}",
            ).exists()
        )

    def test_activate_queued_package(self):
        pkg = _package(self.creator, status="queued")
        self.assertTrue(pkg.activate())
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_activate_open_package_noop(self):
        pkg = _package(self.creator, status="open")
        self.assertFalse(pkg.activate())
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_recipient_address_prefers_user(self):
        pkg = _package(self.creator, self.recipient)
        self.assertEqual(pkg.recipient_address, self.recipient.email)

    def test_recipient_address_falls_back_to_email(self):
        pkg = _package(self.creator, recipient=None, recipient_email="ext@test.com")
        self.assertEqual(pkg.recipient_address, "ext@test.com")


class StripeAccountModelTest(TestCase):
    def test_is_active(self):
        user = _user("stripe@test.com")
        acct = _stripe_account(user, active=True)
        self.assertTrue(acct.is_active)

    def test_is_inactive(self):
        user = _user("stripe2@test.com")
        acct = _stripe_account(user, active=False)
        self.assertFalse(acct.is_active)


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


class MarkAsPaidTransactionTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.payer = _user("payer@test.com")

    def test_records_marked_reimbursed_atomically(self):
        pkg = _package(self.creator, self.payer)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.00"))
        pkg.records.add(r1, r2)
        pkg.mark_as_paid(self.payer)
        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertTrue(r1.reimbursed)
        self.assertTrue(r2.reimbursed)


class PackageBusinessLogicTest(TestCase):
    """Covers the domain methods moved out of the views."""

    def setUp(self):
        self.creator = _user("creator@test.com")
        _stripe_account(self.creator)
        self.recipient = _user("recipient@test.com")
        self.pkg = _package(self.creator, self.recipient, title="Biz Logic")

    def test_payout_account_id_returns_active_account(self):
        self.assertEqual(self.pkg.payout_account_id, "acct_test123")

    def test_payout_account_id_none_when_inactive(self):
        _stripe_account(self.creator, active=False)
        self.assertIsNone(self.pkg.payout_account_id)

    def test_can_be_paid_by_allows_recipient(self):
        ok, err = self.pkg.can_be_paid_by(self.recipient)
        self.assertTrue(ok)
        self.assertIsNone(err)

    def test_can_be_paid_by_rejects_creator(self):
        ok, err = self.pkg.can_be_paid_by(self.creator)
        self.assertFalse(ok)
        self.assertIn("own", err)

    def test_can_be_paid_by_rejects_expired(self):
        self.pkg.expires_at = timezone.now() - timedelta(days=1)
        self.pkg.save(update_fields=["expires_at"])
        ok, _err = self.pkg.can_be_paid_by(self.recipient)
        self.assertFalse(ok)

    def test_can_be_paid_by_rejects_paid(self):
        self.pkg.mark_as_paid(self.recipient)
        ok, _err = self.pkg.can_be_paid_by(self.recipient)
        self.assertFalse(ok)

    def test_can_be_paid_by_rejects_when_no_payout_account(self):
        _stripe_account(self.creator, active=False)
        ok, err = self.pkg.can_be_paid_by(self.recipient)
        self.assertFalse(ok)
        self.assertIn("payouts", err)

    @patch("reimbursements.models.get_rates", return_value={})
    def test_build_line_items(self, _mock_rates):
        r1 = _record(self.creator, Decimal("25.00"))
        r2 = _record(self.creator, Decimal("5.00"))
        self.pkg.records.add(r1, r2)
        items = self.pkg.build_line_items("usd")
        self.assertEqual(len(items.line_items), 2)
        self.assertEqual(items.total_cents, 3000)
        self.assertEqual(items.total_amount, Decimal("30.00"))

    @patch("reimbursements.models.get_rates", return_value={})
    def test_build_line_items_empty_when_nothing_payable(self, _mock_rates):
        r = _record(self.creator, Decimal("0.00"))
        self.pkg.records.add(r)
        items = self.pkg.build_line_items("usd")
        self.assertEqual(items.line_items, [])
        self.assertEqual(items.total_cents, 0)

    def test_platform_fee_cents_normal(self):
        fee = self.pkg.platform_fee_cents(10000, "usd", {"USD": Decimal("1")})
        self.assertEqual(fee, 300)

    def test_platform_fee_cents_minimum_floor(self):
        fee = self.pkg.platform_fee_cents(1000, "usd", {"USD": Decimal("1")})
        self.assertEqual(fee, 50)

    def test_platform_fee_cents_capped_at_total(self):
        fee = self.pkg.platform_fee_cents(20, "usd", {"USD": Decimal("1")})
        self.assertEqual(fee, 20)

    def test_lock_for_payment_open(self):
        self.assertIsNotNone(self.pkg.lock_for_payment())

    def test_lock_for_payment_paid_returns_none(self):
        self.pkg.mark_as_paid(self.recipient)
        self.assertIsNone(self.pkg.lock_for_payment())

    @patch("reimbursements.services.retrieve_checkout_session")
    def test_resumable_session_url_open(self, mock_retrieve):
        PackagePayment.objects.create(
            package=self.pkg,
            payer=self.recipient,
            stripe_checkout_session_id="cs_open",
            amount_paid=Decimal("30.00"),
        )
        mock_retrieve.return_value.status = "open"
        mock_retrieve.return_value.url = "https://checkout.stripe.com/foo"
        self.assertEqual(self.pkg.resumable_session_url(), "https://checkout.stripe.com/foo")

    @patch("reimbursements.services.retrieve_checkout_session")
    def test_resumable_session_url_completed(self, mock_retrieve):
        PackagePayment.objects.create(
            package=self.pkg,
            payer=self.recipient,
            stripe_checkout_session_id="cs_done",
            amount_paid=Decimal("30.00"),
        )
        mock_retrieve.return_value.status = "complete"
        self.assertIsNone(self.pkg.resumable_session_url())

    @patch("reimbursements.services.retrieve_checkout_session")
    def test_resumable_session_url_stripe_error_falls_back(self, mock_retrieve):
        PackagePayment.objects.create(
            package=self.pkg,
            payer=self.recipient,
            stripe_checkout_session_id="cs_err",
            amount_paid=Decimal("30.00"),
        )
        mock_retrieve.side_effect = stripe.error.StripeError("boom")
        self.assertIsNone(self.pkg.resumable_session_url())

    def test_create_for_attaches_records(self):
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.00"))
        records = Record.objects.filter(id__in=[r1.id, r2.id], user=self.creator, is_active=True)
        pkg = ReimbursementPackage.objects.create_for(
            creator=self.creator,
            recipient=self.recipient,
            title="From Manager",
            records=records,
            days_valid=14,
        )
        self.assertEqual(pkg.title, "From Manager")
        self.assertEqual(pkg.records.count(), 2)
        self.assertEqual(pkg.currency, "usd")
        self.assertLess(pkg.expires_at, timezone.now() + timedelta(days=15))

    @patch("reimbursements.models.get_rates", return_value={})
    def test_detail_items(self, _mock_rates):
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("5.00"))
        self.pkg.records.add(r1, r2)
        detail = self.pkg.detail_items("usd")
        self.assertEqual(len(detail.record_items), 2)
        self.assertEqual(detail.converted_total, Decimal("15.00"))
        self.assertEqual(detail.original_total, Decimal("15.00"))

    @patch("reimbursements.models.get_rates", return_value={})
    def test_prefetch_converted_totals(self, _mock_rates):
        r1 = _record(self.creator, Decimal("12.50"))
        self.pkg.records.add(r1)
        packages = list(
            ReimbursementPackage.objects.filter(pk=self.pkg.pk).with_prefetched_active_records()
        )
        ReimbursementPackage.prefetch_converted_totals(packages, "usd")
        self.assertEqual(packages[0].display_total, Decimal("12.50"))


class PackagePaymentModelTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.payer = _user("payer@test.com")
        self.pkg = _package(self.creator, self.payer)
        self.payment = PackagePayment.objects.create(
            package=self.pkg,
            payer=self.payer,
            stripe_checkout_session_id="cs_pay",
            amount_paid=Decimal("50.00"),
        )

    def test_amount_matches(self):
        session = {"id": "cs_pay", "amount_total": 5000, "currency": "usd"}
        self.assertTrue(self.payment.amount_matches(session))

    def test_amount_mismatch_rejected(self):
        session = {"id": "cs_pay", "amount_total": 9000, "currency": "usd"}
        self.assertFalse(self.payment.amount_matches(session))

    def test_currency_mismatch_rejected(self):
        session = {"id": "cs_pay", "amount_total": 5000, "currency": "eur"}
        self.assertFalse(self.payment.amount_matches(session))

    def test_complete_from_session_dict(self):
        self.payment.complete_from_session({"payment_intent": "pi_123"})
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_completed)
        self.assertEqual(self.payment.stripe_payment_intent_id, "pi_123")

    def test_complete_from_session_object(self):
        session = type("Session", (), {"payment_intent": "pi_obj"})()
        self.payment.complete_from_session(session)
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_completed)
        self.assertEqual(self.payment.stripe_payment_intent_id, "pi_obj")

    def test_mark_failed(self):
        self.payment.is_completed = True
        self.payment.save(update_fields=["is_completed"])
        self.payment.mark_failed()
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_completed)


# ---------------------------------------------------------------------------
# Webhook tests
# ---------------------------------------------------------------------------
class StripeWebhookTest(TestCase):
    def _event(self, event_type, payload, event_id="evt_test"):
        return {"id": event_id, "type": event_type, "data": {"object": payload}}

    @patch("reimbursements.webhooks.transaction.on_commit", side_effect=lambda fn: fn())
    @patch("reimbursements.webhooks._notify_package_paid")
    def test_checkout_session_completed(self, mock_notify, mock_on_commit):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_test123",
            amount_paid=Decimal("50.00"),
        )

        event = self._event(
            "checkout.session.completed",
            {
                "id": "cs_test123",
                "payment_status": "paid",
                "payment_intent": "pi_test123",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {"package_uuid": str(pkg.uuid)},
            },
        )
        process_stripe_event(event)

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(payment.stripe_payment_intent_id, "pi_test123")
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        mock_notify.assert_called_once_with(pkg.pk, payer.pk)

    def test_checkout_session_not_paid_is_noop(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_unpaid",
            amount_paid=Decimal("50.00"),
        )

        process_stripe_event(
            self._event(
                "checkout.session.completed",
                {
                    "id": "cs_unpaid",
                    "payment_status": "unpaid",
                    "metadata": {"package_uuid": str(pkg.uuid)},
                },
            )
        )

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_checkout_session")
    def test_checkout_session_missing_payment_raises_on_transient_error(self, mock_retrieve):
        mock_retrieve.side_effect = stripe.error.StripeError("Network error")

        with self.assertRaises(PackagePayment.DoesNotExist):
            process_stripe_event(
                self._event(
                    "checkout.session.completed",
                    {
                        "id": "cs_nonexistent",
                        "payment_status": "paid",
                        "metadata": {"package_uuid": "00000000-0000-0000-0000-000000000000"},
                    },
                )
            )

    @patch("reimbursements.services.retrieve_checkout_session")
    def test_checkout_session_unknown_to_stripe_is_skipped(self, mock_retrieve):
        mock_retrieve.side_effect = stripe.error.InvalidRequestError(
            "No such checkout session: cs_nonexistent", "id"
        )

        process_stripe_event(
            self._event(
                "checkout.session.completed",
                {
                    "id": "cs_nonexistent",
                    "payment_status": "paid",
                    "metadata": {"package_uuid": "00000000-0000-0000-0000-000000000000"},
                },
            )
        )

    def test_checkout_session_completed_is_idempotent(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_test123",
            amount_paid=Decimal("50.00"),
        )

        event = self._event(
            "checkout.session.completed",
            {
                "id": "cs_test123",
                "payment_status": "paid",
                "amount_total": 5000,
                "currency": "usd",
                "metadata": {"package_uuid": str(pkg.uuid)},
            },
            event_id="evt_dup",
        )
        process_stripe_event(event)
        process_stripe_event(event)

        self.assertEqual(AuditLog.objects.filter(details__event="package_paid").count(), 1)

    def test_async_payment_succeeded(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_async",
            amount_paid=Decimal("50.00"),
        )

        process_stripe_event(
            self._event(
                "checkout.session.async_payment_succeeded",
                {
                    "id": "cs_async",
                    "payment_intent": "pi_async",
                    "amount_total": 5000,
                    "currency": "usd",
                    "metadata": {"package_uuid": str(pkg.uuid)},
                },
            )
        )

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

    def test_account_updated(self):
        user = _user("stripe@test.com")
        user.stripe_account.stripe_account_id = "acct_test"
        user.stripe_account.save(update_fields=["stripe_account_id"])

        process_stripe_event(
            self._event(
                "account.updated",
                {
                    "id": "acct_test",
                    "details_submitted": True,
                    "charges_enabled": True,
                    "payouts_enabled": False,
                },
            )
        )

        user.stripe_account.refresh_from_db()
        self.assertTrue(user.stripe_account.stripe_details_submitted)
        self.assertTrue(user.stripe_account.charges_enabled)
        self.assertFalse(user.stripe_account.payouts_enabled)

    def test_charge_refunded_full(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_refund_test",
            stripe_payment_intent_id="pi_refund_test",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        process_stripe_event(
            self._event(
                "charge.refunded",
                {
                    "payment_intent": "pi_refund_test",
                    "amount_refunded": 5000,
                    "amount_captured": 5000,
                },
            )
        )

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_charge_refunded_partial_keeps_package_paid(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_refund_partial",
            stripe_payment_intent_id="pi_refund_partial",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        process_stripe_event(
            self._event(
                "charge.refunded",
                {
                    "payment_intent": "pi_refund_partial",
                    "amount_refunded": 2500,
                    "amount_captured": 5000,
                },
            )
        )

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertFalse(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

    @patch("reimbursements.services.retrieve_charge")
    def test_transfer_failed_resolves_via_source_charge(self, mock_retrieve):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_transfer_test",
            stripe_payment_intent_id="pi_transfer_test",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        mock_retrieve.return_value = {"payment_intent": "pi_transfer_test"}

        process_stripe_event(
            self._event(
                "transfer.failed",
                {
                    "id": "tr_transfer_test",
                    "source_transaction": "ch_transfer_test",
                    "failure_message": "Insufficient funds",
                },
            )
        )

        mock_retrieve.assert_called_once_with("ch_transfer_test")
        payment = PackagePayment.objects.get(stripe_checkout_session_id="cs_transfer_test")
        self.assertFalse(payment.is_completed)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_transfer_failed_without_resolvable_payment_is_noop(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        process_stripe_event(
            self._event("transfer.failed", {"id": "tr_unknown", "failure_message": "unknown"})
        )

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

    def test_charge_failed_marks_package_refunded(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_charge_fail",
            stripe_payment_intent_id="pi_charge_fail",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        process_stripe_event(
            self._event(
                "charge.failed",
                {
                    "id": "ch_charge_fail",
                    "payment_intent": "pi_charge_fail",
                    "failure_message": "Card declined",
                },
            )
        )

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertFalse(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_payment_intent")
    def test_charge_refunded_resolves_via_payment_intent_metadata(self, mock_pi):
        """Reversal events route to the payment even before the completed event runs."""
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_refund_race",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )
        mock_pi.return_value = {
            "id": "pi_refund_race",
            "metadata": {"package_uuid": str(pkg.uuid)},
        }

        process_stripe_event(
            self._event(
                "charge.refunded",
                {
                    "payment_intent": "pi_refund_race",
                    "amount_refunded": 5000,
                    "amount_captured": 5000,
                },
            )
        )

        payment = PackagePayment.objects.get(stripe_checkout_session_id="cs_refund_race")
        self.assertFalse(payment.is_completed)
        self.assertEqual(payment.stripe_payment_intent_id, "pi_refund_race")
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_payment_intent")
    def test_charge_failed_resolves_via_payment_intent_metadata(self, mock_pi):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_fail_race",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )
        mock_pi.return_value = {
            "id": "pi_fail_race",
            "metadata": {"package_uuid": str(pkg.uuid)},
        }

        process_stripe_event(
            self._event(
                "charge.failed",
                {
                    "payment_intent": "pi_fail_race",
                    "failure_message": "Card declined",
                },
            )
        )

        payment = PackagePayment.objects.get(stripe_checkout_session_id="cs_fail_race")
        self.assertFalse(payment.is_completed)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_created_reverts_package(self, mock_charge):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_dispute",
            stripe_payment_intent_id="pi_dispute",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )
        mock_charge.return_value = {"payment_intent": "pi_dispute"}

        process_stripe_event(
            self._event(
                "charge.dispute.created",
                {
                    "id": "dp_1",
                    "charge": "ch_dispute",
                    "amount": 5000,
                    "currency": "usd",
                    "status": "needs_response",
                },
            )
        )

        mock_charge.assert_called_once_with("ch_dispute")
        payment = PackagePayment.objects.get(stripe_checkout_session_id="cs_dispute")
        self.assertFalse(payment.is_completed)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_closed_lost_reverts_package(self, mock_charge):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_dispute_lost",
            stripe_payment_intent_id="pi_dispute_lost",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )
        mock_charge.return_value = {"payment_intent": "pi_dispute_lost"}

        process_stripe_event(
            self._event(
                "charge.dispute.closed",
                {
                    "id": "dp_2",
                    "charge": "ch_dispute_lost",
                    "amount": 5000,
                    "currency": "usd",
                    "status": "lost",
                },
            )
        )

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_won_restores_package(self, mock_charge):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.mark_as_refunded()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_dispute_won",
            stripe_payment_intent_id="pi_dispute_won",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )
        payment.mark_failed()
        mock_charge.return_value = {"payment_intent": "pi_dispute_won"}

        process_stripe_event(
            self._event(
                "charge.dispute.closed",
                {
                    "id": "dp_3",
                    "charge": "ch_dispute_won",
                    "amount": 5000,
                    "currency": "usd",
                    "status": "won",
                },
            )
        )

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(AuditLog.objects.filter(details__event="charge_dispute_won").count(), 1)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_unknown_charge_is_noop(self, mock_charge):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        mock_charge.side_effect = stripe.error.InvalidRequestError(
            "No such charge: ch_unknown", "id"
        )

        process_stripe_event(
            self._event(
                "charge.dispute.created",
                {
                    "id": "dp_4",
                    "charge": "ch_unknown",
                    "amount": 5000,
                    "currency": "usd",
                    "status": "needs_response",
                },
            )
        )

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)


class _FakeSession:
    """Stands in for a stripe.CheckoutSession in reconciliation tests."""

    def __init__(self, **data):
        self._data = data
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict_recursive(self):
        return dict(self._data)


class WebhookPipelineResilienceTest(TestCase):
    """The reimbursements pipeline must not silently drop webhook events."""

    def test_error_receiver_enqueues_handled_event(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        trigger = mock.Mock()
        trigger.json_body = {"type": "checkout.session.completed"}
        with patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error(instance=trigger)
        send.assert_called_once_with(trigger.id)

    def test_error_receiver_skips_unhandled_event(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        trigger = mock.Mock()
        trigger.json_body = {"type": "customer.subscription.updated"}
        with patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error(instance=trigger)
        send.assert_not_called()

    def test_error_receiver_noop_without_instance(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        with patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error()
        send.assert_not_called()


class ApplyPaidSessionTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.payer = _user("payer@test.com")
        self.pkg = _package(self.creator, self.payer)
        self.payment = PackagePayment.objects.create(
            package=self.pkg,
            payer=self.payer,
            stripe_checkout_session_id="cs_apply",
            amount_paid=Decimal("50.00"),
        )

    @patch("reimbursements.webhooks.transaction.on_commit", side_effect=lambda fn: fn())
    @patch("reimbursements.webhooks._notify_package_paid")
    def test_applies_paid_session(self, mock_notify, mock_on_commit):
        session = {
            "id": "cs_apply",
            "payment_intent": "pi_apply",
            "amount_total": 5000,
            "currency": "usd",
        }
        self.assertTrue(apply_paid_session(self.payment, session, source="payment_synced"))
        self.payment.refresh_from_db()
        self.pkg.refresh_from_db()
        self.assertTrue(self.payment.is_completed)
        self.assertEqual(self.payment.stripe_payment_intent_id, "pi_apply")
        self.assertEqual(self.pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(AuditLog.objects.filter(details__event="payment_synced").count(), 1)
        mock_notify.assert_called_once_with(self.pkg.pk, self.payer.pk)

    def test_amount_mismatch_is_noop(self):
        session = {"id": "cs_apply", "amount_total": 9000, "currency": "usd"}
        self.assertFalse(apply_paid_session(self.payment, session, source="payment_synced"))
        self.payment.refresh_from_db()
        self.pkg.refresh_from_db()
        self.assertFalse(self.payment.is_completed)
        self.assertEqual(self.pkg.status, ReimbursementPackage.Status.OPEN)

    def test_accepts_stripe_object(self):
        session = _FakeSession(
            id="cs_apply",
            payment_intent="pi_obj",
            amount_total=5000,
            currency="usd",
        )
        self.assertTrue(apply_paid_session(self.payment, session, source="payment_synced"))
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_completed)
        self.assertEqual(self.payment.stripe_payment_intent_id, "pi_obj")

    @patch("reimbursements.webhooks.transaction.on_commit", side_effect=lambda fn: fn())
    @patch("reimbursements.webhooks._notify_package_paid")
    def test_already_completed_does_not_notify_again(self, mock_notify, mock_on_commit):
        self.payment.is_completed = True
        self.payment.save(update_fields=["is_completed"])
        self.pkg.mark_as_paid(self.payer)
        self.pkg.refresh_from_db()

        session = {"id": "cs_apply", "amount_total": 5000, "currency": "usd"}
        self.assertTrue(apply_paid_session(self.payment, session, source="payment_synced"))
        self.assertEqual(AuditLog.objects.filter(details__event="payment_synced").count(), 0)
        mock_notify.assert_not_called()


class SyncPaymentStatusTest(TestCase):
    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    @patch("reimbursements.webhooks.transaction.on_commit", side_effect=lambda fn: fn())
    @patch("reimbursements.webhooks._notify_package_paid")
    def test_sync_marks_paid_with_audit(self, mock_notify, mock_on_commit, mock_retrieve):
        from reimbursements.tasks import sync_payment_status

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_sync",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.return_value = _FakeSession(
            id="cs_sync",
            payment_status="paid",
            payment_intent="pi_sync",
            amount_total=5000,
            currency="usd",
        )

        sync_payment_status.fn(str(pkg.uuid), payment.pk)

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(AuditLog.objects.filter(details__event="payment_synced").count(), 1)
        mock_notify.assert_called_once_with(pkg.pk, payer.pk)

    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    def test_sync_amount_mismatch_is_noop(self, mock_retrieve):
        from reimbursements.tasks import sync_payment_status

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_sync_mismatch",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.return_value = _FakeSession(
            id="cs_sync_mismatch",
            payment_status="paid",
            amount_total=9999,
            currency="usd",
        )

        sync_payment_status.fn(str(pkg.uuid), payment.pk)

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertFalse(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)
        self.assertEqual(AuditLog.objects.filter(details__event="payment_synced").count(), 0)

    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    def test_sync_skips_unpaid_session(self, mock_retrieve):
        from reimbursements.tasks import sync_payment_status

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_sync_unpaid",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.return_value = _FakeSession(
            id="cs_sync_unpaid",
            payment_status="open",
            amount_total=5000,
            currency="usd",
        )

        sync_payment_status.fn(str(pkg.uuid), payment.pk)

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)


class ReconcilePendingPaymentsTaskTest(TestCase):
    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    @patch("reimbursements.webhooks.transaction.on_commit", side_effect=lambda fn: fn())
    @patch("reimbursements.webhooks._notify_package_paid")
    def test_reconcile_marks_paid(self, mock_notify, mock_on_commit, mock_retrieve):
        from reimbursements.tasks import reconcile_pending_payments_task

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_recon",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.return_value = _FakeSession(
            id="cs_recon",
            payment_status="paid",
            payment_intent="pi_recon",
            amount_total=5000,
            currency="usd",
        )

        reconcile_pending_payments_task.fn()

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(AuditLog.objects.filter(details__event="payment_synced").count(), 1)
        mock_notify.assert_called_once_with(pkg.pk, payer.pk)

    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    def test_reconcile_skips_unpaid_sessions(self, mock_retrieve):
        from reimbursements.tasks import reconcile_pending_payments_task

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_recon_open",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.return_value = _FakeSession(
            id="cs_recon_open",
            payment_status="open",
            amount_total=5000,
            currency="usd",
        )

        reconcile_pending_payments_task.fn()

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    def test_reconcile_continues_past_failures(self, mock_retrieve):
        from reimbursements.tasks import reconcile_pending_payments_task

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg1 = _package(creator, payer)
        pkg2 = _package(creator, payer)
        PackagePayment.objects.create(
            package=pkg1,
            payer=payer,
            stripe_checkout_session_id="cs_recon_bad",
            amount_paid=Decimal("50.00"),
        )
        PackagePayment.objects.create(
            package=pkg2,
            payer=payer,
            stripe_checkout_session_id="cs_recon_good",
            amount_paid=Decimal("50.00"),
        )
        mock_retrieve.side_effect = lambda session_id, **kw: _reconcile_session(
            session_id, bad="cs_recon_bad", good="cs_recon_good"
        )

        reconcile_pending_payments_task.fn()

        pkg1.refresh_from_db()
        pkg2.refresh_from_db()
        self.assertEqual(pkg1.status, ReimbursementPackage.Status.OPEN)
        self.assertEqual(pkg2.status, ReimbursementPackage.Status.PAID)

    @patch("reimbursements.tasks.services.retrieve_checkout_session")
    def test_reconcile_ignores_paid_packages(self, mock_retrieve):
        from reimbursements.tasks import reconcile_pending_payments_task

        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer, status="paid")
        PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_recon_paid",
            amount_paid=Decimal("50.00"),
        )

        reconcile_pending_payments_task.fn()

        mock_retrieve.assert_not_called()


class ReimbursementRecordAccessTest(TestCase):
    """Purpose-bound, temporary record access granted to package recipients."""

    def setUp(self):
        self.creator = _user("andy@test.com")
        self.recipient = _user("sarah@test.com")
        self.other_record = _record(self.creator)

    def _package_with_records(self):
        r1 = _record(self.creator)
        r2 = _record(self.creator)
        pkg, _ = services.create_reimbursement_package(
            creator=self.creator,
            recipient_email=self.recipient.email,
            record_ids=[r1.pk, r2.pk],
            title="Lunch receipts",
            days_valid=7,
        )
        return pkg, r1, r2

    def test_create_grants_temporary_view_access(self):
        pkg, r1, r2 = self._package_with_records()
        for r in (r1, r2):
            share = RecordShare.objects.get(record=r, user=self.recipient)
            self.assertEqual(share.permission, RecordShare.Permission.VIEW)
            self.assertEqual(share.purpose, RecordShare.Purpose.REIMBURSEMENT)
            self.assertTrue(share.include_documents)
            self.assertEqual(share.expires_at, pkg.expires_at)
            self.assertIsNone(share.revoked_at)

    def test_create_only_grants_packaged_records(self):
        _, r1, r2 = self._package_with_records()
        visible = set(Record.objects.visible_to(self.recipient).values_list("pk", flat=True))
        self.assertIn(r1.pk, visible)
        self.assertIn(r2.pk, visible)
        self.assertNotIn(self.other_record.pk, visible)
        self.assertFalse(
            RecordShare.objects.filter(record=self.other_record, user=self.recipient).exists()
        )

    def test_mark_as_paid_revokes_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.mark_as_paid(self.recipient)
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNotNone(share.revoked_at)
        self.assertFalse(share.is_active)
        self.assertNotIn(
            r1.pk, Record.objects.visible_to(self.recipient).values_list("pk", flat=True)
        )

    def test_refund_restores_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.mark_as_paid(self.recipient)
        pkg.mark_as_refunded()
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNone(share.revoked_at)
        self.assertTrue(share.is_active)
        self.assertIn(r1.pk, Record.objects.visible_to(self.recipient).values_list("pk", flat=True))

    def test_deleted_package_revokes_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.delete_package(self.creator)
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNotNone(share.revoked_at)


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
