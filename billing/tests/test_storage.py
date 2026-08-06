from unittest import mock

import stripe
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import entitlements, metadata
from .helpers import FakeSession


class StoragePackEntitlementTests(TestCase):
    """Pro plan + 25GB storage pack should show a 50GB limit."""

    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_pro", livemode=False, created=timezone.now()
        )
        self.user = get_user_model().objects.create_user(
            username="storage_buyer",
            email="buyer@example.com",
            password="password",
        )
        self.user.customer = self.customer
        self.user.save()

        pro_product = Product.objects.create(
            id=metadata.PAPERTRAIL_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Papertrail Pro",
        )
        pro_price = Price.objects.create(
            id="price_pro",
            livemode=False,
            active=True,
            product=pro_product,
            currency="usd",
        )
        self.pro_sub = Subscription.objects.create(
            id="sub_pro",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_pro",
            livemode=False,
            created=timezone.now(),
            subscription=self.pro_sub,
            price=pro_price,
        )
        self.user.subscription = self.pro_sub
        self.user.save()

    def _make_storage_sub(
        self,
        customer,
        sub_id="sub_storage",
        price_id="price_storage",
        product_meta=None,
    ):
        product_meta = product_meta or metadata.STORAGE_UPGRADE_25
        storage_product, _ = Product.objects.get_or_create(
            id=product_meta.stripe_id,
            defaults={
                "livemode": False,
                "active": True,
                "name": product_meta.name,
            },
        )
        storage_price = Price.objects.create(
            id=price_id,
            livemode=False,
            active=True,
            product=storage_product,
            currency="usd",
        )
        storage_sub = Subscription.objects.create(
            id=sub_id,
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_" + sub_id,
            livemode=False,
            created=timezone.now(),
            subscription=storage_sub,
            price=storage_price,
        )
        return storage_sub

    def test_multiple_storage_addons_do_not_stack(self):
        self._make_storage_sub(self.customer, sub_id="sub_25", price_id="price_25")
        self._make_storage_sub(
            self.customer,
            sub_id="sub_100",
            price_id="price_100",
            product_meta=metadata.STORAGE_UPGRADE_100,
        )
        self.assertEqual(entitlements.get_storage_limit(self.user), 125)
        self.assertEqual(
            [a.stripe_id for a in metadata.storage_addons_for_user(self.user)],
            [metadata.STORAGE_UPGRADE_100.stripe_id],
        )
        self.assertEqual(
            " + ".join(p.name for p in metadata.active_products_for_user(self.user)),
            "Papertrail Pro + 100GB Storage Upgrade",
        )

    def test_limit_is_50_when_addon_shares_customer(self):
        self._make_storage_sub(self.customer)
        self.assertEqual(entitlements.get_storage_limit(self.user), 50)
        self.assertEqual(
            [a.stripe_id for a in metadata.storage_addons_for_user(self.user)],
            [metadata.STORAGE_UPGRADE_25.stripe_id],
        )
        self.assertEqual(
            " + ".join(p.name for p in metadata.active_products_for_user(self.user)),
            "Papertrail Pro + 25GB Storage Upgrade",
        )

    def test_limit_is_50_when_addon_on_stray_customer(self):
        stray = Customer.objects.create(
            id="cus_stray",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self._make_storage_sub(stray)
        self.assertEqual(entitlements.get_storage_limit(self.user), 50)


class StoragePackConfirmFlowTests(TestCase):
    """End-to-end subscription_confirm with a mocked Stripe sync."""

    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_pro", livemode=False, created=timezone.now()
        )
        self.user = get_user_model().objects.create_user(
            username="flow_buyer",
            email="flow@example.com",
            password="password",
        )
        self.user.customer = self.customer
        self.user.save()

        pro_product = Product.objects.create(
            id=metadata.PAPERTRAIL_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Papertrail Pro",
        )
        pro_price = Price.objects.create(
            id="price_pro",
            livemode=False,
            active=True,
            product=pro_product,
            currency="usd",
        )
        self.pro_sub = Subscription.objects.create(
            id="sub_pro",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_pro",
            livemode=False,
            created=timezone.now(),
            subscription=self.pro_sub,
            price=pro_price,
        )
        self.user.subscription = self.pro_sub
        self.user.save()

    def _run_confirm(self, storage_sub, session_customer):
        patchers = [
            mock.patch(
                "billing.services.retrieve_checkout_session",
                return_value=FakeSession(
                    customer=session_customer,
                    customer_details={"email": self.user.email},
                ),
            ),
            mock.patch(
                "billing.services.retrieve_subscription",
                return_value={"id": storage_sub.id, "items": {"data": []}},
            ),
            mock.patch(
                "billing.models.stripe.Subscription.retrieve",
                return_value={"id": storage_sub.id, "items": {"data": []}},
            ),
            mock.patch(
                "billing.views.Subscription.sync_from_stripe_data",
                return_value=storage_sub,
            ),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(mock.patch.stopall)
        self.client.force_login(self.user)
        return self.client.get(reverse("subscription_confirm"), {"session_id": "cs_storage"})

    def test_confirm_flow_adds_storage_limit(self):
        storage_sub = self._make_storage_sub()
        response = self._run_confirm(storage_sub, session_customer="cus_pro")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(entitlements.get_storage_limit(self.user), 50)
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id,
            metadata.PAPERTRAIL_PRO.stripe_id,
        )

    def test_create_checkout_reuses_existing_customer(self):
        with (
            mock.patch("billing.views.Customer.get_or_create") as get_or_create,
            mock.patch("billing.services.create_checkout_session") as session_create,
            mock.patch(
                "billing.services.stripe.Customer.retrieve",
                return_value=type("R", (), {"deleted": False})(),
            ),
        ):
            get_or_create.return_value = (Customer(id="cus_brand_new"), True)
            session_create.return_value = FakeSession(subscription=None)
            self.client.force_login(self.user)
            self.client.post(reverse("create_checkout_session"), {"base_price_id": "price_pro"})
            session_create.assert_called_once()
            called_customer = session_create.call_args.kwargs["customer"]
            self.assertEqual(called_customer, self.customer.id)
        get_or_create.assert_not_called()

    def test_checkout_falls_back_when_customer_deleted_in_stripe(self):
        with (
            mock.patch("billing.views.Customer.get_or_create") as get_or_create,
            mock.patch("billing.services.create_checkout_session") as session_create,
            mock.patch(
                "billing.services.stripe.Customer.retrieve",
                side_effect=stripe.error.InvalidRequestError(
                    message="No such customer", param="id"
                ),
            ),
        ):
            fresh = Customer.objects.create(id="cus_fresh", livemode=False, created=timezone.now())
            get_or_create.return_value = (fresh, True)
            session_create.return_value = FakeSession(subscription=None)
            self.client.force_login(self.user)
            self.client.post(reverse("create_checkout_session"), {"base_price_id": "price_pro"})
            session_create.assert_called_once()
            self.assertEqual(session_create.call_args.kwargs["customer"], "cus_fresh")
            self.user.refresh_from_db()
            self.assertEqual(self.user.customer_id, fresh.djstripe_id)

    def _make_storage_sub(self):
        storage_product = Product.objects.create(
            id=metadata.STORAGE_UPGRADE_25.stripe_id,
            livemode=False,
            active=True,
            name="25GB Storage Upgrade",
        )
        storage_price = Price.objects.create(
            id="price_storage",
            livemode=False,
            active=True,
            product=storage_product,
            currency="usd",
        )
        storage_sub = Subscription.objects.create(
            id="sub_storage",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_storage",
            livemode=False,
            created=timezone.now(),
            subscription=storage_sub,
            price=storage_price,
        )
        return storage_sub
