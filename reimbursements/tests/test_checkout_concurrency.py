"""Concurrency tests for the package checkout claim path.

Exercises the row-lock + pending-payment claim in "create_package_checkout"
with real database transactions and simultaneous callers. Two checkouts must
never both reach Stripe session creation — previously they could both be
charged with transfers on both PaymentIntents.

select_for_update is a no-op on SQLite, so these tests are skipped there;
CI runs them against PostgreSQL.
"""

import threading
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, TransactionTestCase

from reimbursements import services
from reimbursements.models import PackagePayment

from ._helpers import _package, _record, _stripe_account, _user

_FAKE_SESSION = SimpleNamespace(
    id="cs_race_1", url="https://checkout.stripe.com/pay/race-1", status="open"
)


@unittest.skipUnless(connection.vendor == "postgresql", "requires PostgreSQL (select_for_update)")
class CheckoutClaimConcurrencyTest(TransactionTestCase):
    """Two simultaneous checkouts for one open package."""

    def setUp(self):
        self.creator = _user("race-creator@test.com")
        self.payer = _user("race-payer@test.com")
        _stripe_account(self.creator, active=True)
        self.pkg = _package(self.creator)
        self.pkg.records.add(_record(self.creator, balance=Decimal("25.00")))

    def _run_two_checkouts(self):
        barrier = threading.Barrier(2)
        outcomes: list = [None, None]

        def attempt(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                outcomes[index] = services.create_package_checkout(
                    package=self.pkg,
                    payer=self.payer,
                    currency="usd",
                    success_url="https://app.test/success",
                    cancel_url="https://app.test/cancel",
                )
            except Exception as e:  # surfaced below via assertion failure
                outcomes[index] = e
            finally:
                connection.close()

        with (
            patch(
                "reimbursements.services.create_checkout_session",
                side_effect=lambda **kwargs: _FAKE_SESSION,
            ) as mock_create,
            patch(
                "reimbursements.services.get_rates",
                return_value={"USD": Decimal("1")},
            ),
            # Locked out thread must find the existing open session reusable
            # instead of hitting the real Stripe API (fake key -> 401) and
            # racing a second session with the same mocked id.
            patch(
                "reimbursements.services.retrieve_checkout_session",
                return_value=_FAKE_SESSION,
            ),
        ):
            threads = [
                threading.Thread(target=attempt, args=(i,), name=f"checkout-{i}") for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        for i, outcome in enumerate(outcomes):
            self.assertNotIsInstance(outcome, Exception, f"thread {i} crashed: {outcome!r}")
            self.assertIsNotNone(outcome, f"thread {i} produced no result")
        return outcomes, mock_create

    def test_only_one_stripe_session_is_created(self):
        outcomes, mock_create = self._run_two_checkouts()

        # Exactly one attempt may reach Stripe, no matter the interleaving.
        self.assertEqual(
            mock_create.call_count,
            1,
            "two concurrent checkouts both reached Stripe session creation",
        )

        payments = list(PackagePayment.objects.all())
        self.assertEqual(len(payments), 1)
        payment = payments[0]
        self.assertEqual(payment.stripe_checkout_session_id, "cs_race_1")

        redirects = {o.redirect_url for o in outcomes if o.redirect_url}
        errors = [o.error for o in outcomes if o.error]
        # One payer proceeds; the other either reuses the same session or is
        # told a checkout is already being prepared. Never a second session.
        self.assertLessEqual(len(redirects), 1)
        self.assertTrue(
            len(errors) == 1 or len(redirects) == 1,
            f"unexpected combined outcomes: {outcomes!r}",
        )


class CheckoutRetryAfterStripeFailureTest(TestCase):
    """A failed session creation must leave the package claimable again."""

    def setUp(self):
        self.creator = _user("retry-creator@test.com")
        self.payer = _user("retry-payer@test.com")
        _stripe_account(self.creator, active=True)
        self.pkg = _package(self.creator)
        self.pkg.records.add(_record(self.creator, balance=Decimal("25.00")))

    @patch("reimbursements.services.get_rates", return_value={"USD": Decimal("1")})
    @patch("reimbursements.services.create_checkout_session")
    def test_failed_attempt_does_not_block_retry(self, mock_create, _mock_rates):
        import stripe

        mock_create.side_effect = stripe.error.APIConnectionError("network down")
        first = services.create_package_checkout(
            package=self.pkg,
            payer=self.payer,
            currency="usd",
            success_url="https://app.test/success",
            cancel_url="https://app.test/cancel",
        )
        self.assertIn("Unable to initiate payment", first.error)
        self.assertEqual(PackagePayment.objects.count(), 0)

        mock_create.side_effect = None
        mock_create.return_value = SimpleNamespace(id="cs_retry_ok", url="https://pay/ok")
        second = services.create_package_checkout(
            package=self.pkg,
            payer=self.payer,
            currency="usd",
            success_url="https://app.test/success",
            cancel_url="https://app.test/cancel",
        )
        self.assertEqual(second.redirect_url, "https://pay/ok")
        self.assertEqual(PackagePayment.objects.count(), 1)
