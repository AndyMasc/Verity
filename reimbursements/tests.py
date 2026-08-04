import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import stripe
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from billing.tests_helpers import give_pro_subscription
from records.models import AuditLog, Record
from reimbursements.models import PackagePayment, ReimbursementPackage, StripeAccount
from reimbursements.views import validate_recipient_email
from reimbursements.webhooks import process_stripe_event

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

    def test_nonexistent_email(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "nobody@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertFalse(data["valid"])
        self.assertNotIn("nobody@test.com", data["error"])

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

    def test_nonexistent_recipient(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Fail",
                    "recipient_email": "ghost@test.com",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

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
