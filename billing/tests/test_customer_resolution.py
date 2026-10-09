"""Resolving the user's Stripe Customer when duplicates are linked.

dj-stripe's Customer.get_or_create() calls .get(subscriber=user) and raises
MultipleObjectsReturned as soon as two Customer rows point at the same user, which
made checkout fail with HTTP 500. These cover the reconciliation that replaced it.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from ..services import resolve_customer


class ResolveCustomerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="dupes", email="dupes@example.com", password="password"
        )

    def _customer(self, suffix, subscriber=True):
        return Customer.objects.create(
            id=f"cus_{suffix}",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user if subscriber else None,
        )

    def test_single_linked_customer_is_returned(self):
        customer = self._customer("one")
        self.assertEqual(resolve_customer(self.user), customer)

    def test_duplicates_do_not_raise(self):
        first = self._customer("first")
        second = self._customer("second")
        resolved = resolve_customer(self.user)
        self.assertIn(resolved.pk, {first.pk, second.pk})
        # Exactly one stays linked, so the next call cannot raise either.
        self.assertEqual(
            Customer.objects.filter(subscriber=self.user).count(),
            1,
        )

    def test_the_users_own_customer_wins(self):
        own = self._customer("own")
        self._customer("other")
        self.user.customer = own
        self.user.save(update_fields=["customer"])
        self.assertEqual(resolve_customer(self.user), own)

    def test_customer_holding_subscriptions_wins(self):
        empty = self._customer("empty")
        busy = self._customer("busy")
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Pro",
            metadata={"category": metadata.BASE_PLAN_CATEGORY},
        )
        price = Price.objects.create(
            id="price_dupes", livemode=False, active=True, product=product, currency="usd"
        )
        Subscription.objects.create(
            id="sub_dupes",
            livemode=False,
            created=timezone.now(),
            customer=busy,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_dupes",
            livemode=False,
            created=timezone.now(),
            subscription=Subscription.objects.get(id="sub_dupes"),
            price=price,
        )
        self.assertEqual(resolve_customer(self.user), busy)
        self.assertIsNotNone(empty)

    def test_resolve_is_idempotent(self):
        self._customer("a")
        self._customer("b")
        first = resolve_customer(self.user)
        second = resolve_customer(self.user)
        self.assertEqual(first.pk, second.pk)

    def test_unlinked_rows_are_left_alone(self):
        """Only rows claiming this user as subscriber are touched."""
        other_user = get_user_model().objects.create_user(
            username="other", email="other@example.com", password="password"
        )
        theirs = Customer.objects.create(
            id="cus_theirs",
            livemode=False,
            created=timezone.now(),
            subscriber=other_user,
        )
        self._customer("mine")
        resolve_customer(self.user)
        self.assertEqual(Customer.objects.get(pk=theirs.pk).subscriber, other_user)
