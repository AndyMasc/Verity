from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Subscription

from reimbursements.webhooks import (
    HANDLED_EVENT_TYPES,
    enqueue_reimbursement_processing,
)

from ..webhooks import (
    handle_subscription_deleted,
    report_webhook_processing_error,
)


def _fake_event(event_type, object_id):
    event = mock.Mock()
    event.type = event_type
    event.data = {"object": {"id": object_id}}
    return event


class HandleSubscriptionDeletedTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_deleted", livemode=False, created=timezone.now()
        )
        self.subscription = Subscription.objects.create(
            id="sub_deleted",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        self.user = get_user_model().objects.create_user(
            username="deleted_user",
            email="deleted@example.com",
            password="password",
        )
        self.user.subscription = self.subscription
        self.user.save()

    def test_clears_user_subscription(self):
        with self.captureOnCommitCallbacks(execute=True):
            handle_subscription_deleted(
                event=_fake_event("customer.subscription.deleted", "sub_deleted")
            )
        self.user.refresh_from_db()
        self.assertIsNone(self.user.subscription_id)

    def test_noop_when_event_missing(self):
        with self.captureOnCommitCallbacks(execute=True):
            handle_subscription_deleted()
        self.user.refresh_from_db()
        self.assertEqual(self.user.subscription_id, self.subscription.djstripe_id)

    def test_noop_when_subscription_unknown(self):
        with self.captureOnCommitCallbacks(execute=True):
            handle_subscription_deleted(
                event=_fake_event("customer.subscription.deleted", "sub_does_not_exist")
            )
        self.user.refresh_from_db()
        self.assertEqual(self.user.subscription_id, self.subscription.djstripe_id)


class EnqueueReimbursementProcessingTests(TestCase):
    def test_enqueues_handled_event(self):
        with mock.patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_reimbursement_processing(
                    instance=mock.Mock(event=_fake_event("charge.refunded", "ch_refunded"))
                )
        send.assert_called_once()

    def test_skips_unhandled_event(self):
        with mock.patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_reimbursement_processing(
                    instance=mock.Mock(event=_fake_event("customer.subscription.updated", "sub_x"))
                )
        send.assert_not_called()

    def test_noop_when_no_instance(self):
        enqueue_reimbursement_processing()


class ReportWebhookProcessingErrorTests(TestCase):
    def test_logs_without_raising(self):
        report_webhook_processing_error(
            instance=mock.Mock(),
            exception=RuntimeError("boom"),
        )

    def test_handled_event_types_are_expected_subset(self):
        self.assertIn("checkout.session.completed", HANDLED_EVENT_TYPES)
        self.assertIn("charge.refunded", HANDLED_EVENT_TYPES)
        self.assertNotIn("customer.subscription.deleted", HANDLED_EVENT_TYPES)
