"""Tests for the Stripe webhook integration and webhook pipeline resilience."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.db import OperationalError
from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import stripe
from django.test import TestCase, TransactionTestCase

from records.models import AuditLog

from reimbursements.models import (
    PackagePayment,
    ProcessedStripeEvent,
    ReimbursementPackage,
)
from reimbursements.webhooks import (
    _EVENT_HANDLERS,
    process_stripe_event,
)

from ._helpers import _FakeSession, _package, _user


class StripeWebhookTest(TestCase):

    def _event(self, event_type, payload, event_id="evt_test"):
        return {
            "id": event_id,
            "type": event_type,
            "data": {
                "object": payload
            }
        }

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
            ))

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
            ))

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
            ))

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        partial_refund_logs = AuditLog.objects.filter(
            details__event="charge_refunded", details__is_full_refund=False)
        self.assertTrue(partial_refund_logs.exists())

    @patch("reimbursements.services.create_refund")
    @patch("reimbursements.services.retrieve_charge")
    def test_transfer_failed_resolves_via_source_charge(
            self, mock_retrieve, mock_refund):
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
            ))

        mock_retrieve.assert_called_once_with("ch_transfer_test")
        payment = PackagePayment.objects.get(
            stripe_checkout_session_id="cs_transfer_test")
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
            self._event("transfer.failed", {
                "id": "tr_unknown",
                "failure_message": "unknown"
            }))

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

    @patch("reimbursements.services.create_refund")
    def test_charge_failed_marks_package_refunded(self, mock_refund):
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
            ))

        # A captured charge must be returned to the payer before reopening.
        mock_refund.assert_called_once()
        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertFalse(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_charge_refunded_reverts_paid_package(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_payment_intent_id="pi_refund_race",
            stripe_invoice_id="in_refund_race",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        process_stripe_event(
            self._event(
                "charge.refunded",
                {
                    "id": "ch_refund_race",
                    "payment_intent": "pi_refund_race",
                    "amount_refunded": 5000,
                },
            ))

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_charge_failed_reverts_paid_package(self):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_payment_intent_id="pi_fail_race",
            stripe_invoice_id="in_fail_race",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        with patch("reimbursements.services.create_refund") as refund:
            refund.side_effect = stripe.error.InvalidRequestError(
                "not capturable", "pi")
            process_stripe_event(
                self._event(
                    "charge.failed",
                    {
                        "id": "ch_fail_race",
                        "payment_intent": "pi_fail_race",
                        "failure_message": "Card declined",
                    },
                ))

        # Auto-refund failed, so the package is deliberately left settled and
        # flagged rather than silently reverted.
        payment.refresh_from_db()
        self.assertTrue(payment.is_completed)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(
            AuditLog.objects.filter(
                details__event="auto_refund_failed").count(), 1)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_created_freezes_package(self, mock_charge):
        """Non-terminal disputes freeze the package instead of reopening it.

        Reverting mid-dispute let a third party fund an open package while the
        original charge was contested; resolution arrives via .closed events.
        """
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
            ))

        mock_charge.assert_called_once_with("ch_dispute")
        payment = PackagePayment.objects.get(
            stripe_checkout_session_id="cs_dispute")
        self.assertTrue(payment.is_completed)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertTrue(
            AuditLog.objects.filter(details__event="charge_dispute_open",
                                    details__dispute_id="dp_1").exists())

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
            ))

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
            ))

        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(
            AuditLog.objects.filter(
                details__event="charge_dispute_won").count(), 1)

    @patch("reimbursements.services.retrieve_charge")
    def test_dispute_unknown_charge_is_noop(self, mock_charge):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()

        mock_charge.side_effect = stripe.error.InvalidRequestError(
            "No such charge: ch_unknown", "id")

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
            ))

        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)


class WebhookPipelineResilienceTest(TestCase):
    """The reimbursements pipeline must not silently drop webhook events."""

    def test_error_receiver_enqueues_handled_event(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        trigger = mock.Mock()
        trigger.event = None
        trigger.json_body = {"type": "invoice.paid"}
        with patch(
                "reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error(instance=trigger)
        send.assert_called_once_with(trigger.id)

    def test_error_receiver_skips_unhandled_event(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        trigger = mock.Mock()
        trigger.json_body = {"type": "customer.subscription.updated"}
        with patch(
                "reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error(instance=trigger)
        send.assert_not_called()

    def test_error_receiver_noop_without_instance(self):
        from reimbursements.webhooks import enqueue_reimbursement_processing_on_error

        with patch(
                "reimbursements.tasks.process_stripe_event_task.send") as send:
            enqueue_reimbursement_processing_on_error()
        send.assert_not_called()


class WebhookIdempotencyTest(TestCase):
    """The event claim must be taken before the handler does anything.

    Check-then-act let two concurrent deliveries both pass the existence
    check and both run the handler: the loser's database work rolled back,
    but the notification it had already enqueued and the refund it had
    already requested from Stripe did not.
    """

    def _event(self, event_id):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_missing",
                    "payment_status": "paid"
                }
            },
        }

    def test_claim_is_recorded_before_the_handler_runs(self):
        observed = []

        def handler(event_data):
            observed.append(
                ProcessedStripeEvent.objects.filter(
                    event_id=event_data["id"]).exists())

        with patch.dict(_EVENT_HANDLERS,
                        {"checkout.session.completed": handler}):
            process_stripe_event(self._event("evt_claim"))

        self.assertEqual(observed, [True])

    def test_redelivery_is_a_clean_skip(self):
        """A duplicate must not raise, so Stripe gets a 200 and stops retrying."""
        calls = []

        with patch.dict(
                _EVENT_HANDLERS,
            {"checkout.session.completed": lambda e: calls.append(e["id"])},
        ):
            process_stripe_event(self._event("evt_dup"))
            process_stripe_event(self._event("evt_dup"))
            process_stripe_event(self._event("evt_dup"))

        self.assertEqual(calls, ["evt_dup"])
        self.assertEqual(
            ProcessedStripeEvent.objects.filter(event_id="evt_dup").count(), 1)

    def test_a_failed_handler_does_not_consume_the_event(self):
        """The claim rolls back with the handler, so Stripe can retry."""
        with patch.dict(
                _EVENT_HANDLERS,
            {
                "checkout.session.completed": mock.Mock(
                    side_effect=RuntimeError("boom"))
            },
        ):
            with self.assertRaises(RuntimeError):
                process_stripe_event(self._event("evt_retry"))

        self.assertFalse(
            ProcessedStripeEvent.objects.filter(event_id="evt_retry").exists())

    def test_an_unhandled_event_type_is_still_claimed(self):
        """Unknown types are recorded so a redelivery stays a no-op."""
        process_stripe_event(self._event("evt_unknown_type"))
        self.assertTrue(
            ProcessedStripeEvent.objects.filter(
                event_id="evt_unknown_type").exists())


def _is_transient_lock(exc: BaseException) -> bool:
    return isinstance(exc, OperationalError) and "locked" in str(exc).lower()


class ConcurrentWebhookDeliveryTest(TransactionTestCase):
    """Two real workers delivering one event must produce exactly one action."""

    def _event(self, event_id):
        return {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_missing",
                    "payment_status": "paid"
                }
            },
        }

    def test_concurrent_delivery_runs_the_handler_exactly_once(self):
        calls = []
        lock = threading.Lock()
        start = threading.Barrier(2)
        errors = []

        def handler(event_data):
            with lock:
                calls.append(event_data["id"])

        def deliver():
            from django.db import connection

            try:
                start.wait(timeout=15)
                for _ in range(5):
                    try:
                        process_stripe_event(self._event("evt_race"))
                    except OperationalError as exc:
                        # SQLite serialises writers with a table lock, so the
                        # losing delivery can hit a transient lock instead of a
                        # clean IntegrityError. Production is PostgreSQL, where
                        # the unique-key conflict is reliable. Retry briefly.
                        if "locked" not in str(exc).lower():
                            raise
                        time.sleep(0.1)
                    else:
                        break
            except Exception as exc:  # noqa: BLE001 - asserted below
                errors.append(exc)
            finally:
                connection.close()

        with patch.dict(_EVENT_HANDLERS,
                        {"checkout.session.completed": handler}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(deliver) for _ in range(2)]
                for f in futures:
                    f.result()

        self.assertEqual(
            [e for e in errors if not _is_transient_lock(e)],
            [],
            "a duplicate delivery must not raise",
        )
        self.assertEqual(len(calls), 1, f"handler ran {len(calls)} times")
        self.assertEqual(
            ProcessedStripeEvent.objects.filter(event_id="evt_race").count(),
            1)


class LocalStripeSyncResolutionTest(TestCase):
    """A charge resolves to its payment without hitting Stripe's API twice.

    dj-stripe syncs the Charge before webhook_post_process fires, and the
    payment is matched on a plain column, so the common path is all-local.
    """

    def setUp(self):
        self.creator, self.recipient = _user("lr_c@e.com"), _user("lr_p@e.com")
        self.package = _package(self.creator, self.recipient)
        self.payment = PackagePayment.objects.create(
            package=self.package,
            payer=self.recipient,
            stripe_invoice_id="in_local",
            stripe_payment_intent_id="pi_local",
            amount_paid=100,
            payer_currency="usd",
        )

    def test_resolves_charge_without_calling_stripe(self):
        from reimbursements.webhooks import payment_from_charge

        with (patch("stripe.Charge.retrieve") as
              charge, patch("stripe.Invoice.retrieve") as invoice):
            charge.return_value = {"id": "ch_local", "invoice": "in_local"}
            found = payment_from_charge("ch_local")

        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.payment.pk)
        # Matched on the stored invoice id, so no second API call is needed.
        self.assertFalse(invoice.called)

    def test_resolves_via_payment_intent_without_calling_stripe(self):
        from reimbursements.webhooks import payment_from_charge

        with (patch("stripe.Charge.retrieve") as
              charge, patch("stripe.Invoice.retrieve")):
            charge.return_value = {"id": "ch_pi", "payment_intent": "pi_local"}
            found = payment_from_charge("ch_pi")

        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.payment.pk)

    def test_unknown_charge_resolves_to_none(self):
        from reimbursements.webhooks import payment_from_charge

        with patch("stripe.Charge.retrieve") as charge:
            charge.return_value = {
                "id": "ch_unknown",
                "payment_intent": "pi_nope"
            }
            self.assertIsNone(payment_from_charge("ch_unknown"))


class InvoiceSettlementTest(TestCase):

    def setUp(self):
        self.creator = _user("inv_creator@test.com")
        self.package = _package(self.creator,
                                recipient_email="payer@outside.com")
        self.payment = PackagePayment.objects.create(
            package=self.package,
            payer=None,
            payer_email="payer@outside.com",
            stripe_checkout_session_id="pending:invoice-test",
            stripe_invoice_id="in_test",
            amount_paid=100,
            expected_amount_cents=10000,
            payer_currency="usd",
        )

    def _invoice(self, amount_paid=10000):
        return {
            "id": "in_test",
            "amount_paid": amount_paid,
            "currency": "usd",
            "payment_intent": "pi_inv",
        }

    def test_invoice_paid_settles_the_package(self):
        from reimbursements.webhooks import handle_invoice_paid

        with patch("reimbursements.webhooks.notify_package_paid") as notify:
            with self.captureOnCommitCallbacks(execute=True):
                handle_invoice_paid({
                    "type": "invoice.paid",
                    "data": {
                        "object": self._invoice()
                    }
                })
        self.package.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(self.package.status, ReimbursementPackage.Status.PAID)
        self.assertTrue(self.payment.is_completed)
        self.assertEqual(self.payment.stripe_payment_intent_id, "pi_inv")
        self.assertTrue(notify.called)

    def test_short_payment_is_refused(self):
        from reimbursements.webhooks import handle_invoice_paid

        handle_invoice_paid({
            "type": "invoice.paid",
            "data": {
                "object": self._invoice(amount_paid=9999)
            }
        })
        self.package.refresh_from_db()
        self.assertNotEqual(self.package.status,
                            ReimbursementPackage.Status.PAID)

    def test_paid_invoice_for_deleted_package_is_refunded(self):
        from reimbursements.webhooks import handle_invoice_paid

        self.package.delete_package(self.creator)
        handle_invoice_paid({
            "type": "invoice.paid",
            "data": {
                "object": self._invoice()
            }
        })
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_completed)

    def test_unknown_invoice_is_ignored(self):
        from reimbursements.webhooks import handle_invoice_paid

        handle_invoice_paid({
            "type": "invoice.paid",
            "data": {
                "object": {
                    "id": "in_unrelated",
                    "amount_paid": 10000
                }
            },
        })
        self.package.refresh_from_db()
        self.assertNotEqual(self.package.status,
                            ReimbursementPackage.Status.PAID)
