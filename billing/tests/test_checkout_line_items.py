"""The line items sent to Stripe Checkout.

Stripe enforces two opposite rules, so these tests assert the outbound payload
rather than the response:

* a licensed line item must carry "quantity"
* a metered line item must not, because usage arrives via the billing meter

Either mistake is a 400 from the API, and neither shows up in a response-only
test because the Stripe call is mocked.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.http import HttpResponseBadRequest
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from .helpers import FakeSession, give_pro_subscription

PRO_PRICE = "price_pro_monthly"
FREE_PRICE = "price_free_monthly"
STORAGE_PRICE = "price_storage_monthly"


class CheckoutLineItemTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="checkout",
            email="checkout@example.com",
            password="password",
        )
        customer = Customer.objects.create(
            id="cus_checkout",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = customer
        self.user.save(update_fields=["customer"])
        self.client.force_login(self.user)
        for meta, price_ids in (
            (metadata.VERITY_PRO, (PRO_PRICE,)),
            (metadata.VERITY_FREE, (FREE_PRICE,)),
        ):
            product = Product.objects.create(
                id=meta.stripe_id,
                livemode=False,
                active=True,
                name=meta.name or meta.stripe_id,
                metadata={"category": "base_plan"},
            )
            for price_id in price_ids:
                price = Price.objects.create(
                    id=price_id,
                    livemode=False,
                    active=True,
                    product=product,
                    currency="usd",
                )
                price.stripe_data = {"recurring": {"interval": "month"}}
                price.save()
        metered = Product.objects.create(
            id=metadata.USAGE_BASED_STORAGE.stripe_id,
            livemode=False,
            active=True,
            name="Usage-Based Storage",
            metadata={"category": "storage_plan"},
        )
        storage_price = Price.objects.create(
            id=STORAGE_PRICE,
            livemode=False,
            active=True,
            product=metered,
            currency="usd",
        )
        storage_price.stripe_data = {"recurring": {"interval": "month"}}
        storage_price.save()

    def _deactivate(self, price_id):
        Price.objects.filter(id=price_id).update(active=False)

    def _checkout(self, *price_ids):
        """Post the pricing form and return the line items handed to Stripe."""
        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch(
                "stripe.checkout.Session.create",
                return_value=FakeSession(),
            ) as create_session,
            mock.patch("djstripe.models.Subscription.update") as update,
        ):
            response = self.client.post(
                reverse("billing:purchase_subscription"),
                {"price_ids": price_ids},
            )
        self.assertEqual(response.status_code, 302)
        if update.called:
            # The user already held a subscription, so it was changed in place.
            return update.call_args.kwargs["items"]
        return create_session.call_args.kwargs["line_items"]

    def test_a_plan_alone_carries_a_quantity(self):
        for price_id in (PRO_PRICE, FREE_PRICE):
            self.assertEqual(self._checkout(price_id), [{"price": price_id, "quantity": 1}])

    def test_metered_line_item_omits_quantity(self):
        (line_item,) = self._checkout(STORAGE_PRICE)
        self.assertNotIn("quantity", line_item)

    def test_plan_and_usage_storage_together(self):
        self.assertEqual(
            self._checkout(PRO_PRICE, STORAGE_PRICE),
            [
                {"price": PRO_PRICE, "quantity": 1},
                {"price": STORAGE_PRICE},
            ],
        )

    def test_two_base_plans_are_rejected(self):
        # One product per category. The category lives on the Product; reading it
        # off the Price resolves every selection to None and rejects valid pairs.
        response = self.client.post(
            reverse("billing:purchase_subscription"),
            {"price_ids": [PRO_PRICE, FREE_PRICE]},
        )
        self.assertEqual(response.status_code, 400)

    def test_free_plan_buys_the_free_metered_rate(self):
        self.assertEqual(
            self._checkout(FREE_PRICE, STORAGE_PRICE),
            [{"price": FREE_PRICE, "quantity": 1}, {"price": STORAGE_PRICE}],
        )

    def test_two_plans_are_rejected(self):
        response = self.client.post(
            reverse("billing:purchase_subscription"),
            {"price_ids": [PRO_PRICE, FREE_PRICE]},
        )
        self.assertEqual(response.status_code, 400)


class SubscriptionUpdateTests(TestCase):
    """Changing an existing subscription rather than opening a second checkout."""

    def setUp(self):
        Product.objects.get_or_create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            defaults={
                "active": True,
                "name": "Verity Pro",
                "metadata": {"category": "base_plan"},
            },
        )
        Product.objects.get_or_create(
            id=metadata.USAGE_BASED_STORAGE.stripe_id,
            livemode=False,
            defaults={
                "active": True,
                "name": "Usage-Based Storage",
                "metadata": {"category": "storage_plan"},
            },
        )
        Price.objects.get_or_create(
            id=STORAGE_PRICE,
            livemode=False,
            defaults={
                "active": True,
                "currency": "usd",
                "product": Product.objects.get(id=metadata.USAGE_BASED_STORAGE.stripe_id),
            },
        )
        self.user = get_user_model().objects.create_user(
            username="updater",
            email="updater@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_updater",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])
        self.client.force_login(self.user)
        Price.objects.create(
            id="price_pro_update",
            livemode=False,
            active=True,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
        )

    def _hold_subscription(self, price_ids):
        subscription = Subscription.objects.create(
            id="sub_update",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        for index, price_id in enumerate(price_ids):
            SubscriptionItem.objects.create(
                id=f"si_update_{index}",
                livemode=False,
                created=timezone.now(),
                subscription=subscription,
                price=Price.objects.get(id=price_id),
            )
        return subscription

    def _post(self, *price_ids):
        with (
            mock.patch(
                "stripe.checkout.Session.create", return_value=FakeSession()
            ) as create_session,
            mock.patch("billing.services._configure"),
        ):
            response = self.client.post(
                reverse("billing:purchase_subscription"), {"price_ids": price_ids}
            )
        return response, create_session

    def test_adding_storage_updates_in_place(self):
        self._hold_subscription(["price_pro_update"])
        with mock.patch.object(Subscription, "update") as update:
            response, create_session = self._post("price_pro_update", STORAGE_PRICE)

        self.assertEqual(response.status_code, 302)
        self.assertFalse(create_session.called, "must not open a second checkout")
        self.assertTrue(update.called)

    def test_reselecting_a_held_price_does_not_duplicate_it(self):
        """Stripe treats an entry without an id as a new item."""
        self._hold_subscription(["price_pro_update"])
        with mock.patch.object(Subscription, "update") as update:
            self._post("price_pro_update", STORAGE_PRICE)

        prices = [item["price"] for item in update.call_args.kwargs["items"] if "price" in item]
        self.assertEqual(len(prices), len(set(prices)), f"duplicate prices: {prices}")
        self.assertEqual(prices, [STORAGE_PRICE])

    def test_unknown_price_id_is_rejected_not_a_crash(self):
        response, _ = self._post("price_does_not_exist")
        self.assertEqual(response.status_code, 400)

    def test_new_customer_without_one_does_not_crash(self):
        """user.customer was None here, so reading user.customer.id used to 500."""
        user = get_user_model().objects.create_user(
            username="brandnew", email="brandnew@example.com", password="password"
        )
        Customer.objects.create(
            id="cus_brandnew", livemode=False, created=timezone.now(), subscriber=user
        )
        self.client.force_login(user)
        with (
            mock.patch("stripe.checkout.Session.create", return_value=FakeSession()),
        ):
            response = self.client.post(
                reverse("billing:purchase_subscription"),
                {"price_ids": ["price_pro_update"]},
            )
        self.assertEqual(response.status_code, 302)


class PurchaseSubscriptionErrorTests(TestCase):
    """purchase_subscription must never redirect to a response object."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="errors", email="errors@example.com", password="password"
        )
        self.customer = Customer.objects.create(
            id="cus_errors",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])
        self.client.force_login(self.user)
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
            metadata={"category": "base_plan"},
        )
        Price.objects.create(
            id="price_pro_errors",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )

    def test_existing_subscription_redirects_to_dashboard(self):
        """The update branch has no checkout_session to redirect to."""
        Subscription.objects.create(
            id="sub_errors",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        with mock.patch("djstripe.models.Subscription.update") as update:
            response = self.client.post(
                reverse("billing:purchase_subscription"),
                {"price_ids": ["price_pro_errors"]},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(update.called)
        self.assertEqual(response.url, reverse("core:dashboard"))


class SuccessUrlPlaceholderTests(TestCase):
    """Stripe substitutes {CHECKOUT_SESSION_ID} only when the braces are literal.

    build_absolute_uri percent-encodes braces to %7B/%7D, so encoding the whole URL
    leaves Stripe with a placeholder it will not fill, and the confirm page then 404s.
    """

    def test_success_url_keeps_the_placeholder_unencoded(self):
        user = get_user_model().objects.create_user(
            username="placeholder", email="placeholder@example.com", password="password"
        )
        customer = Customer.objects.create(
            id="cus_placeholder",
            livemode=False,
            created=timezone.now(),
            subscriber=user,
        )
        user.customer = customer
        user.save(update_fields=["customer"])
        self.client.force_login(user)
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
            metadata={"category": "base_plan"},
        )
        Price.objects.create(
            id="price_pro_placeholder",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )

        with mock.patch(
            "stripe.checkout.Session.create", return_value=FakeSession()
        ) as create_session:
            self.client.post(
                reverse("billing:purchase_subscription"),
                {"price_ids": ["price_pro_placeholder"]},
            )

        success_url = create_session.call_args.kwargs["success_url"]
        self.assertIn("session_id={CHECKOUT_SESSION_ID}", success_url)
        self.assertNotIn("%7B", success_url)


class UpdateSubscriptionPayloadTests(TestCase):
    """The items array handed to Stripe by update_customer_subscription.

    Stripe reads that array as the complete desired state: an entry without an id
    adds a new line item, so re-selecting something already held would double bill.
    """

    def setUp(self):
        Product.objects.get_or_create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            defaults={
                "active": True,
                "name": "Verity Pro",
                "metadata": {"category": "base_plan"},
            },
        )
        Product.objects.get_or_create(
            id=metadata.USAGE_BASED_STORAGE.stripe_id,
            livemode=False,
            defaults={
                "active": True,
                "name": "Usage-Based Storage",
                "metadata": {"category": "storage_plan"},
            },
        )
        Price.objects.get_or_create(
            id=STORAGE_PRICE,
            livemode=False,
            defaults={
                "active": True,
                "currency": "usd",
                "product": Product.objects.get(id=metadata.USAGE_BASED_STORAGE.stripe_id),
            },
        )
        self.user = get_user_model().objects.create_user(
            username="payload", email="payload@example.com", password="password"
        )
        customer = Customer.objects.create(
            id="cus_payload",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = customer
        self.user.save(update_fields=["customer"])
        self.client.force_login(self.user)
        Price.objects.create(
            id="price_pro_payload",
            livemode=False,
            active=True,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
        )
        self.subscription = Subscription.objects.create(
            id="sub_payload",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )

    def _hold(self, item_id, price_id):
        SubscriptionItem.objects.create(
            id=item_id,
            livemode=False,
            created=timezone.now(),
            subscription=self.subscription,
            price=Price.objects.get(id=price_id),
        )

    def _items(self, *price_ids):
        with mock.patch.object(Subscription, "update") as update:
            response = self.client.post(
                reverse("billing:purchase_subscription"), {"price_ids": price_ids}
            )
        self.assertEqual(response.status_code, 302)
        return update.call_args.kwargs["items"]

    def test_metered_item_carries_no_quantity(self):
        self._hold("si_pro", "price_pro_payload")
        items = self._items("price_pro_payload", STORAGE_PRICE)
        self.assertTrue(all("quantity" not in item for item in items))

    def test_every_stale_item_in_a_replaced_category_is_deleted(self):
        # A webhook-synced duplicate can leave two items in one category. Deleting
        # only the first leaves the orphan on the subscription and still billing.
        Price.objects.create(
            id="price_pro_dup",
            livemode=False,
            active=True,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
        )
        self._hold("si_pro_a", "price_pro_payload")
        self._hold("si_pro_b", "price_pro_dup")
        self.assertEqual(
            self._items("price_pro_payload"),
            [{"id": "si_pro_a"}, {"id": "si_pro_b", "deleted": True}],
        )


class NewestSubscriptionChosenTests(TestCase):
    """dj-stripe's Subscription has no Meta.ordering, so a bare .first() resolves to
    the lowest pk -- the oldest row. A price change can leave a newer active row, and
    updating the stale one silently diverges the two.
    """
