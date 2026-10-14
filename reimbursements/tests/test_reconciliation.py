"""Tests for the daily Stripe reconciliation safety net."""

from decimal import Decimal
from unittest.mock import patch

import stripe
from django.test import TestCase

from records.models import AuditLog

from reimbursements.models import PackagePayment, ReimbursementPackage
from reimbursements.reconciliation import run_daily_reconciliation

from ._helpers import _package, _user


def _intent(**overrides):
    data = {"id": "pi_ok", "status": "succeeded", "amount": 5000, "currency": "usd"}
    data.update(overrides)
    return data


class DailyReconciliationTest(TestCase):
    def setUp(self):
        self.creator = _user("recon-creator@test.com")
        self.payer = _user("recon-payer@test.com")

    def _completed_payment(self, pkg, *, pi="pi_ok", expected_cents=None):
        return PackagePayment.objects.create(
            package=pkg,
            payer=self.payer,
            stripe_checkout_session_id=f"cs_{pi}",
            stripe_payment_intent_id=pi,
            amount_paid=Decimal("50.00"),
            expected_amount_cents=expected_cents,
            payer_currency="usd",
            is_completed=True,
        )

    @patch("reimbursements.services.retrieve_payment_intent", return_value=_intent())
    def test_clean_state_produces_no_drift(self, _mock_intent):
        pkg = _package(self.creator, status="paid")
        self._completed_payment(pkg)

        drifts = run_daily_reconciliation()
        self.assertEqual(drifts, [])
        self.assertFalse(AuditLog.objects.exists())

    @patch(
        "reimbursements.services.retrieve_payment_intent",
        return_value=_intent(amount=4000),
    )
    def test_settled_amount_mismatch_is_flagged(self, _mock_intent):
        pkg = _package(self.creator, status="paid")
        payment = self._completed_payment(pkg, expected_cents=5000)
        payment.package.mark_as_paid(payer=self.payer)  # no-op: already paid

        run_daily_reconciliation()
        entry = AuditLog.objects.get(details__drift_kind="settled_amount_mismatch")
        self.assertEqual(entry.details["stripe_amount_cents"], 4000)
        self.assertEqual(entry.details["expected_cents"], 5000)

    @patch("reimbursements.services.retrieve_payment_intent", return_value=_intent())
    def test_paid_package_without_completed_payment_is_flagged(self, _mock_intent):
        _package(self.creator, status="paid")  # no payments at all

        run_daily_reconciliation()
        self.assertTrue(
            AuditLog.objects.filter(
                details__drift_kind="paid_package_without_completed_payment"
            ).exists()
        )

    @patch("reimbursements.services.retrieve_payment_intent", return_value=_intent())
    def test_completed_payment_on_unpaid_package_is_flagged(self, _mock_intent):
        pkg = _package(self.creator, status="open")
        self._completed_payment(pkg)

        run_daily_reconciliation()
        self.assertTrue(
            AuditLog.objects.filter(
                details__drift_kind="completed_payment_on_unpaid_package"
            ).exists()
        )

    @patch("reimbursements.services.retrieve_payment_intent", return_value=_intent())
    def test_missing_intent_at_stripe_is_flagged(self, mock_intent):
        mock_intent.side_effect = stripe.error.InvalidRequestError(
            "No such payment_intent: 'pi_gone'", "id"
        )
        pkg = _package(self.creator, status="paid")
        self._completed_payment(pkg, pi="pi_gone")

        run_daily_reconciliation()
        self.assertTrue(
            AuditLog.objects.filter(details__drift_kind="payment_intent_missing").exists()
        )

    @patch("reimbursements.services.retrieve_payment_intent", return_value=_intent())
    def test_same_drift_is_not_flagged_twice_within_window(self, _mock_intent):
        _package(self.creator, status="paid")  # drift: no completed payment

        first = run_daily_reconciliation()
        second = run_daily_reconciliation()

        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1, "second run should still report the drift")
        self.assertEqual(
            AuditLog.objects.filter(
                details__drift_kind="paid_package_without_completed_payment"
            ).count(),
            1,
            "audit dedup failed — retries would spam the trail",
        )

    @patch("reimbursements.services.retrieve_payment_intent")
    def test_transient_stripe_error_raises_for_retry(self, mock_intent):
        mock_intent.side_effect = stripe.error.APIConnectionError("network down")
        pkg = _package(self.creator, status="paid")
        self._completed_payment(pkg)

        with self.assertRaises(stripe.error.StripeError):
            run_daily_reconciliation()
        self.assertFalse(AuditLog.objects.exists())
