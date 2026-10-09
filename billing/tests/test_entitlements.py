from math import ceil
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import (
    Customer,
    Feature,
    Price,
    Product,
    ProductFeature,
    Subscription,
    SubscriptionItem,
)

from .. import entitlements, features, metadata
from ..models import CustomUser
from .helpers import add_subscription, give_pro_subscription


class EntitlementTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_ent", livemode=False, created=timezone.now()
        )
        self.user = get_user_model().objects.create_user(
            username="plan",
            email="plan@example.com",
            password="password",
        )

    def _add_subscription(self, status="active", product_id=None):
        add_subscription(self.user, self.customer, status=status, product_id=product_id)

    def _attach_feature(self, product_id, lookup_key):
        Product.objects.get_or_create(
            id=product_id,
            livemode=False,
            defaults={"active": True, "name": product_id, "metadata": {"category": "base_plan"}},
        )
        feature = Feature.objects.create(
            id=f"feat_{lookup_key}",
            livemode=False,
            stripe_data={"lookup_key": lookup_key, "name": lookup_key},
        )
        ProductFeature.objects.create(
            id=f"pf_{lookup_key}",
            livemode=False,
            product_id=product_id,
            entitlement_feature=feature,
        )

    def test_free_plan_holds_no_stripe_features(self):
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_FREE.stripe_id
        )
        self.assertEqual(metadata.granted_features(self.user), set())
        self.assertFalse(entitlements.has_feature(self.user, "transaction-sync"))

    def test_pro_product_grants_its_attached_features(self):
        self._attach_feature(metadata.VERITY_PRO.stripe_id, "transaction-sync")
        self._attach_feature(metadata.VERITY_PRO.stripe_id, "record-sharing")
        self._add_subscription(status="active", product_id=metadata.VERITY_PRO.stripe_id)

        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)
        self.assertEqual(
            metadata.granted_features(self.user),
            {"transaction-sync", "record-sharing"},
        )
        self.assertTrue(entitlements.has_feature(self.user, "transaction-sync"))
        self.assertTrue(entitlements.has_feature(self.user, "record-sharing"))

    def test_feature_on_a_product_the_user_does_not_hold_is_not_granted(self):
        self._attach_feature(metadata.VERITY_PRO.stripe_id, "transaction-sync")
        self._add_subscription(status="active", product_id=metadata.USAGE_BASED_STORAGE.stripe_id)
        self.assertFalse(entitlements.has_feature(self.user, "transaction-sync"))

    def test_feature_on_the_metered_addon_is_granted_when_held(self):
        self._attach_feature(metadata.USAGE_BASED_STORAGE.stripe_id, "transaction-sync")
        self._add_subscription(status="active", product_id=metadata.USAGE_BASED_STORAGE.stripe_id)
        self.assertTrue(entitlements.has_feature(self.user, "transaction-sync"))
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_FREE.stripe_id
        )

    def test_trialing_counts_as_paid(self):
        self._add_subscription(status="trialing", product_id=metadata.VERITY_PRO.stripe_id)
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)

    def test_canceled_subscription_is_free(self):
        self._add_subscription(status="canceled", product_id=metadata.VERITY_PRO.stripe_id)
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_FREE.stripe_id
        )
        self.assertFalse(entitlements.has_feature(self.user, "transaction-sync"))

    def test_cancel_at_period_end_keeps_paid_access_until_cycle_end(self):
        self._attach_feature(metadata.VERITY_PRO.stripe_id, "transaction-sync")
        sub = Subscription.objects.create(
            id="sub_ent_cancel_at_period_end",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "canceled", "cancel_at_period_end": True},
        )
        product = Product.objects.get(id=metadata.VERITY_PRO.stripe_id)
        price = Price.objects.get_or_create(
            id=f"price_{metadata.VERITY_PRO.stripe_id}",
            livemode=False,
            defaults={"active": True, "product": product, "currency": "usd"},
        )[0]
        SubscriptionItem.objects.create(
            id="si_cancel_at_period_end",
            livemode=False,
            created=timezone.now(),
            subscription=sub,
            price=price,
        )
        sub.customer.subscriber = self.user
        sub.customer.save(update_fields=["subscriber"])
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)
        self.assertTrue(entitlements.has_feature(self.user, "transaction-sync"))

    def test_unauthenticated_user_has_no_features(self):
        self.assertFalse(entitlements.has_feature(None, "transaction-sync"))


class ContextProcessorTests(TestCase):
    def setUp(self):
        # The subscription context is cached per user id (Redis), and user
        # primary keys repeat across rollback-isolated tests, so start each
        # test with a clean cache to avoid cross-test pollution.
        from django.core.cache import cache

        cache.clear()
        self.customer = Customer.objects.create(id="cus_cp", livemode=False, created=timezone.now())
        self.user = get_user_model().objects.create_user(
            username="cp",
            email="cp@example.com",
            password="password",
        )

    def _request(self):
        from django.test import RequestFactory

        request = RequestFactory().get("/")
        request.user = self.user
        return request

    def test_anon_context(self):
        from django.contrib.auth.models import AnonymousUser

        from ..context_processors import subscription_status

        request = self._request()
        request.user = AnonymousUser()
        ctx = subscription_status(request)
        self.assertFalse(ctx["is_subscribed"])
        self.assertEqual(
            metadata.plan_for_user(request.user).stripe_id,
            metadata.VERITY_FREE.stripe_id,
        )
        self.assertEqual(ctx["plan_name"], metadata.VERITY_FREE.name)

    def test_subscription_with_non_active_status_is_free(self):
        from ..context_processors import subscription_status

        self._add_subscription(status="canceled")
        ctx = subscription_status(self._request())
        self.assertFalse(ctx["is_subscribed"])
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_FREE.stripe_id
        )

    def test_paid_plan_name_renders(self):
        from ..context_processors import subscription_status

        give_pro_subscription(self.user)
        self.assertEqual(subscription_status(self._request())["plan_name"], "Verity Pro")

    def _add_subscription(self, status="active", product_id=None):
        add_subscription(self.user, self.customer, status=status, product_id=product_id)

    def _attach_feature(self, product_id, lookup_key):
        Product.objects.get_or_create(
            id=product_id,
            livemode=False,
            defaults={"active": True, "name": product_id, "metadata": {"category": "base_plan"}},
        )
        feature = Feature.objects.create(
            id=f"feat_{lookup_key}",
            livemode=False,
            stripe_data={"lookup_key": lookup_key, "name": lookup_key},
        )
        ProductFeature.objects.create(
            id=f"pf_{lookup_key}",
            livemode=False,
            product_id=product_id,
            entitlement_feature=feature,
        )


class ProSubscriptionResolvesTests(TestCase):
    """A Pro subscriber must resolve to Pro.

    "plan_for_user" once read Stripe's category off the ProductMetadata dataclass,
    which has no such attribute, so it always matched nothing and every subscriber
    was treated as Free.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="resolved",
            email="resolved@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_resolved", livemode=False, created=timezone.now()
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])

    def _subscribe(self, meta, category):
        product = Product.objects.create(
            id=meta.stripe_id,
            livemode=False,
            active=True,
            name=meta.name or meta.stripe_id,
            metadata={"category": category},
        )
        price = Price.objects.create(
            id=f"price_resolved_{meta.stripe_id}",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        subscription = Subscription.objects.create(
            id=f"sub_resolved_{meta.stripe_id}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id=f"si_resolved_{meta.stripe_id}",
            livemode=False,
            created=timezone.now(),
            subscription=subscription,
            price=price,
        )

    def test_pro_subscriber_resolves_to_pro(self):
        self._subscribe(metadata.VERITY_PRO, "base_plan")
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_PRO)
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)
        self.assertEqual(
            entitlements.get_monthly_upload_limit(self.user, "scan"),
            metadata.VERITY_PRO.monthly_scan_limit,
        )

    def test_metered_only_subscriber_stays_free(self):
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_FREE)


class MeteredIsNotAPlanTests(TestCase):
    """Usage-based storage is priced like a plan but grants no plan features.

    Every product shares the "base_plan" category, so category alone cannot pick
    the plan: without an explicit metered check, a free user who bought only
    usage-based storage resolved to the paid feature set.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="metered",
            email="metered@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_metered", livemode=False, created=timezone.now()
        )

    def _subscribe_usage_storage(self):
        add_subscription(
            self.user,
            self.customer,
            status="active",
            product_id=metadata.USAGE_BASED_STORAGE.stripe_id,
        )

    def test_usage_storage_alone_leaves_the_user_free(self):
        self._subscribe_usage_storage()
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_FREE.stripe_id
        )
        self.assertTrue(metadata.has_metered_storage(self.user))

    def test_usage_storage_alone_grants_no_paid_features(self):
        self._subscribe_usage_storage()
        self.assertEqual(metadata.granted_features(self.user), set())

    def test_plan_resolves_alongside_usage_storage(self):
        add_subscription(
            self.user,
            self.customer,
            status="active",
            product_id=metadata.VERITY_PRO.stripe_id,
        )
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_PRO)
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)


class OrphanedSubscriptionItemTests(TestCase):
    """dj-stripe leaves SubscriptionItem rows behind for items removed from a
    subscription. Reading the item table alone reports the removed item as an
    active product, so items are reconciled against the synced stripe_data.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="orphan", email="orphan@example.com", password="password"
        )
        self.customer = Customer.objects.create(
            id="cus_orphan",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])

        pro = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        storage = Product.objects.create(
            id=metadata.USAGE_BASED_STORAGE.stripe_id,
            livemode=False,
            active=True,
            name="Usage-Based Storage",
        )
        self.pro_price = Price.objects.create(
            id="price_pro_orphan",
            livemode=False,
            active=True,
            product=pro,
            currency="usd",
        )
        self.storage_price = Price.objects.create(
            id="price_storage_orphan",
            livemode=False,
            active=True,
            product=storage,
            currency="usd",
        )
        # Stripe reports only the Pro item; the storage item was removed upstream.
        self.subscription = Subscription.objects.create(
            id="sub_orphan",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={
                "status": "active",
                "items": {"data": [{"price": {"id": self.pro_price.id}}]},
            },
        )
        for item_id, price in (
            ("si_pro_orphan", self.pro_price),
            ("si_storage_orphan", self.storage_price),
        ):
            SubscriptionItem.objects.create(
                id=item_id,
                livemode=False,
                created=timezone.now(),
                subscription=self.subscription,
                price=price,
            )

    def test_removed_item_is_not_an_active_product(self):
        ids = [m.stripe_id for m in metadata.held_products(self.user).values()]
        self.assertEqual(ids, [metadata.VERITY_PRO.stripe_id])

    def test_removed_metered_item_does_not_grant_unlimited_storage(self):
        self.assertFalse(metadata.has_metered_storage(self.user))

    def test_plan_still_resolves(self):
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, metadata.VERITY_PRO.stripe_id)

    def test_unsynced_row_falls_back_to_the_item_table(self):
        # A row never synced with an items payload carries nothing to reconcile
        # against, so its table is used rather than reporting nothing.
        self.subscription.stripe_data = {"status": "active"}
        self.subscription.save(update_fields=["stripe_data"])
        ids = sorted(m.stripe_id for m in metadata.held_products(self.user).values())
        self.assertEqual(
            ids,
            sorted([metadata.VERITY_PRO.stripe_id, metadata.USAGE_BASED_STORAGE.stripe_id]),
        )
