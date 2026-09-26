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


class HandleSubscriptionChangedTests(TestCase):
    def test_cancels_pro_only_storage_when_base_plan_ends(self):
        customer = Customer.objects.create(
            id="cus_cancelled_base", livemode=False, created=timezone.now()
        )
        pro_product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        pro_price = Price.objects.create(
            id="price_pro_cancel",
            livemode=False,
            active=True,
            product=pro_product,
            currency="usd",
        )
        pro_sub = Subscription.objects.create(
            id="sub_pro_cancel",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_pro_cancel",
            livemode=False,
            created=timezone.now(),
            subscription=pro_sub,
            price=pro_price,
        )

        storage_product = Product.objects.create(
            id=metadata.STORAGE_UPGRADE_10.stripe_id,
            livemode=False,
            active=True,
            name="10 GB Storage Pack",
        )
        storage_price = Price.objects.create(
            id="price_storage_cancel",
            livemode=False,
            active=True,
            product=storage_product,
            currency="usd",
        )
        storage_sub = Subscription.objects.create(
            id="sub_storage_cancel",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_storage_cancel",
            livemode=False,
            created=timezone.now(),
            subscription=storage_sub,
            price=storage_price,
        )

        with mock.patch("billing.webhooks.services.cancel_subscription") as cancel_mock:
            handle_subscription_changed(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": pro_sub.id,
                            "customer": customer.id,
                            "status": "canceled",
                            "cancel_at_period_end": False,
                            "items": {
                                "data": [
                                    {
                                        "price": {
                                            "product": metadata.VERITY_PRO.stripe_id
                                        }
                                    }
                                ]
                            },
                        }
                    }
                )
            )

        cancel_mock.assert_called_once_with(storage_sub.id)


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
                    "items": {
                        "data": [{"price": {"product": metadata.VERITY_PRO.stripe_id}}]
                    },
                }
            }
        )

    def _handle_deleted(self, cancel_at_period_end):
        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch("billing.webhooks._cancel_pro_only_storage_for_customer"),
        ):
            webhooks.handle_subscription_deleted(
                event=self._deleted_event(cancel_at_period_end)
            )

    def test_immediate_cancel_tracked_once(self):
        self._handle_deleted(cancel_at_period_end=False)
        self.assertEqual(len(self.captured), 1)
        name, kwargs = self.captured[0]
        self.assertEqual(name, ("subscription_cancelled",))
        self.assertEqual(kwargs["distinct_id"], str(self.user.pk))
        self.assertEqual(kwargs["properties"]["plan"], "Verity Pro")
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
        with mock.patch("billing.webhooks._cancel_pro_only_storage_for_customer"):
            webhooks.handle_subscription_changed(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": self.subscription.id,
                            "status": "canceled",
                            "cancel_at_period_end": False,
                            "items": {
                                "data": [
                                    {
                                        "price": {
                                            "product": metadata.VERITY_PRO.stripe_id
                                        }
                                    }
                                ]
                            },
                        }
                    }
                )
            )
        self.assertEqual(self.captured, [])


class EnqueueReimbursementProcessingTests(TestCase):
    def test_enqueues_handled_event(self):
        with mock.patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_reimbursement_processing(
                    instance=mock.Mock(
                        event=_fake_event("charge.refunded", "ch_refunded")
                    )
                )
        send.assert_called_once()

    def test_skips_unhandled_event(self):
        with mock.patch("reimbursements.tasks.process_stripe_event_task.send") as send:
            with self.captureOnCommitCallbacks(execute=True):
                enqueue_reimbursement_processing(
                    instance=mock.Mock(
                        event=_fake_event("customer.subscription.updated", "sub_x")
                    )
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
