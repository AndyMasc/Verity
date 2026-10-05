from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from reimbursements.webhooks import (
    HANDLED_EVENT_TYPES,
    enqueue_reimbursement_processing,
)

from .. import metadata
from .. import webhooks
from ..webhooks import (
    handle_subscription_changed,
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


class HandleSubscriptionCancellationTrackingTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="cancel_user",
            email="cancel@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_cancel_track", livemode=False, created=timezone.now()
        )
        self.customer.subscriber = self.user
        self.customer.save()
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        price = Price.objects.create(
            id="price_cancel_track",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        self.subscription = Subscription.objects.create(
            id="sub_cancel_track",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_cancel_track",
            livemode=False,
            created=timezone.now(),
            subscription=self.subscription,
            price=price,
        )
        self.captured = []
        self.client_patch = mock.patch.object(
            webhooks,
            "posthog_client",
            mock.Mock(capture=lambda *a, **k: self.captured.append((a, k))),
        )
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def _deleted_event(self, cancel_at_period_end):
        return mock.Mock(
            data={
                "object": {
                    "id": self.subscription.id,
                    "customer": self.customer.id,
                    "cancel_at_period_end": cancel_at_period_end,
                    "items": {"data": [{"price": {"product": metadata.VERITY_PRO.stripe_id}}]},
                }
            }
        )

    def _handle_deleted(self, cancel_at_period_end):
        with self.captureOnCommitCallbacks(execute=True):
            webhooks.handle_subscription_deleted(event=self._deleted_event(cancel_at_period_end))

    def test_immediate_cancel_tracked_once(self):
        self._handle_deleted(cancel_at_period_end=False)
        self.assertEqual(len(self.captured), 1)
        name, kwargs = self.captured[0]
        self.assertEqual(name, ("subscription_cancelled",))
        self.assertEqual(kwargs["distinct_id"], str(self.user.pk))
        self.assertEqual(kwargs["properties"]["cancel_type"], "immediate")

    def test_period_end_cancel_tracked_once(self):
        self._handle_deleted(cancel_at_period_end=True)
        self.assertEqual(len(self.captured), 1)
        self.assertEqual(self.captured[0][1]["properties"]["cancel_type"], "period_end")

    def test_personless_when_customer_unlinked(self):
        self.customer.subscriber = None
        self.customer.save()
        self._handle_deleted(cancel_at_period_end=False)
        self.assertEqual(len(self.captured), 1)
        self.assertNotIn("distinct_id", self.captured[0][1])

    def test_changed_event_does_not_track_cancellation(self):
        webhooks.handle_subscription_changed(
            event=mock.Mock(
                data={
                    "object": {
                        "id": self.subscription.id,
                        "status": "canceled",
                        "cancel_at_period_end": False,
                        "items": {"data": [{"price": {"product": metadata.VERITY_PRO.stripe_id}}]},
                    }
                }
            )
        )
        # subscription_updated is expected here; a *cancellation* is not, since
        # customer.subscription.deleted owns that event.
        self.assertNotIn("base_subscription_cancelled", [name[0] for name, _ in self.captured])

    def test_scheduled_cancel_tracked_when_scheduled(self):
        """A period-end cancel is recorded on .updated, the only event that says so."""
        webhooks.handle_subscription_changed(
            event=mock.Mock(
                data={
                    "object": {
                        "id": self.subscription.id,
                        "status": "active",
                        "customer": self.customer.id,
                        "cancel_at_period_end": True,
                        "items": {"data": [{"price": {"product": metadata.VERITY_PRO.stripe_id}}]},
                    }
                }
            )
        )
        cancels = [
            (name, kwargs) for name, kwargs in self.captured if name[0] == "subscription_cancelled"
        ]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0][1]["properties"]["cancel_type"], "period_end")


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


class CheckoutCompletedTests(TestCase):
    """A settled checkout captures once, whether or not Stripe synced the subscription.

    A real checkout.session.completed payload has no line_items, so a test that
    builds one from session["line_items"] proves nothing.
    """

    def setUp(self):
        from billing import metadata as billing_metadata

        product_id = billing_metadata.VERITY_PRO.stripe_id
        self.product = Product.objects.create(
            id=product_id, stripe_data={"id": product_id, "object": "product"}
        )
        self.price = Price.objects.create(
            id="price_plan",
            product=self.product,
            livemode=False,
            active=True,
            currency="usd",
            stripe_data={"id": "price_plan", "object": "price"},
        )

    def _settle(self, subscription_id):
        from billing import webhooks

        captured = []
        with (
            mock.patch.object(webhooks, "capture", lambda *a, **k: captured.append((a, k))),
            mock.patch.object(
                webhooks,
                "_event_object",
                return_value={
                    "id": "cs_1",
                    "mode": "subscription",
                    "payment_status": "paid",
                    "customer": "cus_1",
                    "client_reference_id": "1",
                    "amount_total": 1000,
                    "currency": "usd",
                    "subscription": subscription_id,
                },
            ),
        ):
            webhooks.handle_checkout_settled(event=mock.Mock())
        return captured

    def test_settled_checkout_is_captured(self):
        customer = Customer.objects.create(
            id="cus_1",
            livemode=False,
            created=timezone.now(),
            stripe_data={"id": "cus_1", "object": "customer"},
        )
        Subscription.objects.create(
            id="sub_1",
            customer=customer,
            livemode=False,
            stripe_data={"id": "sub_1", "object": "subscription"},
        )
        Subscription.objects.get(id="sub_1").items.create(id="si_1", price=self.price)
        captured = self._settle("sub_1")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0][0], "subscription_checkout_completed")

    def test_missing_subscription_does_not_break_the_capture(self):
        captured = self._settle("sub_absent")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][0][0], "subscription_checkout_completed")
