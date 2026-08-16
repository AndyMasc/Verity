"""Tests for reimbursements background tasks (payment sync and reconciliation)."""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from records.models import AuditLog

from reimbursements.models import PackagePayment, ReimbursementPackage

from ._helpers import _FakeSession, _package, _reconcile_session, _user


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
