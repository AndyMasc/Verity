"""The hourly usage sync that feeds Stripe's billing meter.

Subscription.status is a property over stripe_data and is not queryable, so a
filter on it raises FieldError before any meter event is sent. That failure mode
is invisible until a customer is under-billed, so the task is exercised here
end-to-end with the Stripe call mocked.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from ..models import CustomUser
from ..tasks import sync_storage_usage_to_stripe


class SyncStorageUsageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="metered",
            email="metered@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_metered",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])

    def _subscribe(self, meta, status="active", category=None, price_id=None):
        product = Product.objects.create(
            id=meta.stripe_id,
            livemode=False,
            active=True,
            name=meta.name,
            metadata={"category": category or metadata.BASE_PLAN_CATEGORY},
        )
        # The task resolves the metered price from the plan the user is on, so the
        # fixture has to carry that exact Stripe price id.
        price = Price.objects.create(
            id=price_id or f"price_sync_{meta.stripe_id}",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        subscription = Subscription.objects.create(
            id=f"sub_sync_{meta.stripe_id}_{status}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": status},
        )
        item = SubscriptionItem.objects.create(
            id=f"si_sync_{meta.stripe_id}_{status}",
            livemode=False,
            created=timezone.now(),
            subscription=subscription,
            price=price,
        )
        return subscription, item

    def _free_metered_price(self):
        return metadata.USAGE_BASED_STORAGE.price_ids[metadata.VERITY_FREE.stripe_id]

    def _run(self):
        with (
            mock.patch("billing.services._configure"),
            mock.patch("billing.tasks._configure"),
            mock.patch("stripe.billing.MeterEvent.create") as create_event,
        ):
            sync_storage_usage_to_stripe.fn()
        return create_event

    def test_task_runs_without_raising(self):
        # Regression: the ORM status filter made this raise FieldError, so no
        # meter event was ever sent.
        self._subscribe(
            metadata.USAGE_BASED_STORAGE,
            category=metadata.STORAGE_PLAN_CATEGORY,
            price_id=self._free_metered_price(),
        )
        self._run()

    def test_metered_subscriber_gets_an_event(self):
        self._subscribe(
            metadata.USAGE_BASED_STORAGE,
            category=metadata.STORAGE_PLAN_CATEGORY,
            price_id=self._free_metered_price(),
        )
        create_event = self._run()
        self.assertEqual(create_event.call_count, 1)
        payload = create_event.call_args.kwargs["payload"]
        self.assertEqual(payload["stripe_customer_id"], self.customer.id)
        self.assertEqual(create_event.call_args.kwargs["event_name"], "storage_usage")

    def test_reported_value_is_whole_megabytes(self):
        self._subscribe(
            metadata.USAGE_BASED_STORAGE,
            category=metadata.STORAGE_PLAN_CATEGORY,
            price_id=self._free_metered_price(),
        )
        # The task reads the denormalised byte counter, so drive the real path.
        CustomUser.objects.filter(pk=self.user.pk).update(storage_used_bytes=10**9)
        create_event = self._run()
        payload = create_event.call_args.kwargs["payload"]
        # 10**9 bytes is one decimal GB, which is 1000 MB on the meter.
        self.assertEqual(payload["value"], "1000")

    def test_plan_subscriber_without_metered_storage_is_skipped(self):
        self._subscribe(metadata.VERITY_PRO)
        self.assertEqual(self._run().call_count, 0)

    def test_cancelled_metered_subscription_is_skipped(self):
        self._subscribe(
            metadata.USAGE_BASED_STORAGE,
            status="canceled",
            category=metadata.STORAGE_PLAN_CATEGORY,
            price_id=self._free_metered_price(),
        )
        self.assertEqual(self._run().call_count, 0)
