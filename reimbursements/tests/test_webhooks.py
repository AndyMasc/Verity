"""Tests for the Stripe webhook integration and webhook pipeline resilience."""

from decimal import Decimal
from unittest import mock
from unittest.mock import patch

import stripe
from django.test import TestCase

from records.models import AuditLog

from reimbursements.models import PackagePayment, ReimbursementPackage
from reimbursements.webhooks import apply_paid_session, process_stripe_event

from ._helpers import _FakeSession, _package, _user


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
