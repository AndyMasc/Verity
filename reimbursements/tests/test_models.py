"""Tests for reimbursements domain models."""

import math
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import stripe
from django.test import TestCase, override_settings
from django.utils import timezone

from records.models import Record

from reimbursements.models import PackageDraft, PackagePayment, ReimbursementPackage

from ._helpers import _package, _record, _stripe_account, _user


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

    @patch("reimbursements.checkout.get_rates", return_value={})
    def test_build_line_items(self, _mock_rates):
        r1 = _record(self.creator, Decimal("25.00"))
        r2 = _record(self.creator, Decimal("5.00"))
        self.pkg.records.add(r1, r2)
        items = self.pkg.build_line_items("usd")
        self.assertEqual(len(items.line_items), 2)
        self.assertEqual(items.total_cents, 3000)
        self.assertEqual(items.total_amount, Decimal("30.00"))

    @patch("reimbursements.checkout.get_rates", return_value={})
    def test_build_line_items_empty_when_nothing_payable(self, _mock_rates):
        r = _record(self.creator, Decimal("0.00"))
        self.pkg.records.add(r)
        items = self.pkg.build_line_items("usd")
        self.assertEqual(items.line_items, [])
        self.assertEqual(items.total_cents, 0)

    def test_platform_fee_covers_stripe_costs_plus_net_margin(self):
        fee = self.pkg.platform_fee_cents(10000, "usd", {"USD": Decimal("1")})
        # $3.20 processing estimate (3.2% of $100) + $0.30 fixed + $1.00 net margin.
        self.assertEqual(fee, 450)

    def test_platform_fee_small_payment_uses_net_floor(self):
        fee = self.pkg.platform_fee_cents(1000, "usd", {"USD": Decimal("1")})
        # 32c processing estimate + 30c fixed + 25c net floor (beats 1% = 10c).
        self.assertEqual(fee, 87)

    def test_platform_fee_never_exceeds_total(self):
        fee = self.pkg.platform_fee_cents(20, "usd", {"USD": Decimal("1")})
        self.assertEqual(fee, 20)

    def test_platform_net_margin_guaranteed_across_sizes(self):
        for total in (500, 2000, 5000, 25000, 100000):
            with self.subTest(total=total):
                fee = self.pkg.platform_fee_cents(total, "usd", {"USD": Decimal("1")})
                if fee == total:
                    continue  # degenerate micro-payment: Stripe costs exceed the charge
                processing_estimate = math.ceil(total * Decimal("0.032")) + 30
                self.assertGreaterEqual(fee - processing_estimate, 25)

    @override_settings(PLATFORM_NET_PERCENT="0.02")
    def test_platform_net_percent_configurable_via_settings(self):
        fee = self.pkg.platform_fee_cents(10000, "usd", {"USD": Decimal("1")})
        # $3.20 processing estimate + $0.30 fixed + 2% net margin ($2.00).
        self.assertEqual(fee, 550)

    def test_platform_fee_zero_decimal_currency(self):
        rates = {"USD": Decimal("1"), "JPY": Decimal("150")}
        fee = self.pkg.platform_fee_cents(1000, "jpy", rates)
        # Processing: ceil(1000 x 3.2%) = 32 units; fixed $0.30 -> JPY 45;
        # net margin: max(ceil(1000 x 1%) = 10, $0.25 -> JPY 38) = 38 units.
        self.assertEqual(fee, 115)

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
            PackageDraft(
                creator=self.creator,
                recipient=self.recipient,
                title="From Manager",
                records=records,
                days_valid=14,
            )
        )
        self.assertEqual(pkg.title, "From Manager")
        self.assertEqual(pkg.records.count(), 2)
        self.assertEqual(pkg.currency, "usd")
        self.assertLess(pkg.expires_at, timezone.now() + timedelta(days=15))

    @patch("reimbursements.checkout.get_rates", return_value={})
    def test_detail_items(self, _mock_rates):
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("5.00"))
        self.pkg.records.add(r1, r2)
        detail = self.pkg.detail_items("usd")
        self.assertEqual(len(detail.record_items), 2)
        self.assertEqual(detail.converted_total, Decimal("15.00"))
        self.assertEqual(detail.original_total, Decimal("15.00"))

    @patch("reimbursements.checkout.get_rates", return_value={})
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
