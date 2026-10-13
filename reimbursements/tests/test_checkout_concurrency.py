"""Concurrency tests for the package checkout claim path.

Exercises the row-lock + pending-payment claim in "create_package_invoice"
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

_FAKE_INVOICE = SimpleNamespace(
    id="in_race_1",
    status="open",
    auto_advance=False,
    hosted_invoice_url="https://invoice.stripe.com/race-1",
)


@unittest.skipUnless(connection.vendor == "postgresql",
                     "requires PostgreSQL (select_for_update)")
class InvoiceClaimConcurrencyTest(TransactionTestCase):
    """Two simultaneous invoices for one open package."""

    def setUp(self):
        self.creator = _user("race-creator@test.com")
        self.payer = _user("race-payer@test.com")
        _stripe_account(self.creator, active=True)
        self.pkg = _package(self.creator)
        self.pkg.records.add(_record(self.creator, balance=Decimal("25.00")))

    def _run_two_invoices(self):
        barrier = threading.Barrier(2)
        outcomes: list = [None, None]

        def attempt(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                outcomes[index] = services.create_package_invoice(
                    package=self.pkg,
                    payer=self.payer,
                    payer_email=self.payer.email,
                    currency="usd",
                )
            except Exception as e:  # surfaced below via assertion failure
                outcomes[index] = e
            finally:
                connection.close()

        with (
                patch(
                    "reimbursements.services.create_invoice",
                    side_effect=lambda *a, **k: _FAKE_INVOICE,
                ) as mock_create,
                patch(
                    "reimbursements.services.get_rates",
                    return_value={"USD": Decimal("1")},
                ),
                patch("reimbursements.services.send_invoice",
                      return_value=_FAKE_INVOICE),
                patch("reimbursements.services.create_invoice_item",
                      return_value=_FAKE_INVOICE),
                patch("reimbursements.services.find_or_create_customer",
                      return_value="cus_x"),
                patch(
                    "reimbursements.services.services_hosted_url",
                    return_value=_FAKE_INVOICE.hosted_invoice_url,
                ),
        ):
            threads = [
                threading.Thread(target=attempt,
                                 args=(i, ),
                                 name=f"invoice-{i}") for i in range(2)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        for i, outcome in enumerate(outcomes):
            self.assertNotIsInstance(outcome, Exception,
                                     f"thread {i} crashed: {outcome!r}")
            self.assertIsNotNone(outcome, f"thread {i} produced no result")
        return outcomes, mock_create

    def test_only_one_invoice_is_created(self):
        outcomes, mock_create = self._run_two_invoices()

        # Exactly one attempt may reach Stripe: a draft create plus its
        # auto_advance finalise. Any higher means both threads got through.
        self.assertEqual(
            mock_create.call_count,
            2,
            "two concurrent invoices both reached Stripe",
        )

        payments = list(PackagePayment.objects.all())
        self.assertEqual(len(payments), 1)
        payment = payments[0]
        self.assertEqual(payment.stripe_invoice_id, "in_race_1")

        redirects = {o.redirect_url for o in outcomes if o.redirect_url}
        errors = [o.error for o in outcomes if o.error]
        # One payer proceeds; the other either reuses the same session or is
        # told a checkout is already being prepared. Never a second session.
        self.assertLessEqual(len(redirects), 1)
        self.assertTrue(
            len(errors) == 1 or len(redirects) == 1,
            f"unexpected combined outcomes: {outcomes!r}",
        )


class InvoiceRetryAfterStripeFailureTest(TestCase):
    """A failed invoice creation must leave the package claimable again."""

    def setUp(self):
        self.creator = _user("retry-creator@test.com")
        self.payer = _user("retry-payer@test.com")
        _stripe_account(self.creator, active=True)
        self.pkg = _package(self.creator)
        self.pkg.records.add(_record(self.creator, balance=Decimal("25.00")))

    @patch("reimbursements.services.create_invoice_item",
           return_value=_FAKE_INVOICE)
    @patch("reimbursements.services.services_hosted_url",
           return_value="https://invoice/x")
    @patch("reimbursements.services.send_invoice", return_value=_FAKE_INVOICE)
    @patch("reimbursements.services.find_or_create_customer",
           return_value="cus_x")
    @patch("reimbursements.services.get_rates",
           return_value={"USD": Decimal("1")})
    @patch("reimbursements.services.create_invoice")
    def test_failed_attempt_does_not_block_retry(self, mock_create, _rates,
                                                 _cust, _send, _url, _item):
        import stripe

        mock_create.side_effect = stripe.error.APIConnectionError(
            "network down")
        first = services.create_package_invoice(package=self.pkg,
                                                payer=self.payer,
                                                payer_email=self.payer.email,
                                                currency="usd")
        self.assertIn("Unable to raise an invoice", first.error)
        self.assertEqual(PackagePayment.objects.count(), 0)

        mock_create.side_effect = None
        mock_create.return_value = _FAKE_INVOICE
        second = services.create_package_invoice(package=self.pkg,
                                                 payer=self.payer,
                                                 payer_email=self.payer.email,
                                                 currency="usd")
        self.assertEqual(second.redirect_url, "https://invoice/x")
        self.assertEqual(PackagePayment.objects.count(), 1)
