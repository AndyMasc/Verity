from math import ceil
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import entitlements, features, metadata
from ..models import CustomUser
from .helpers import add_subscription


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

    def test_free_plan_features(self):
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertEqual(entitlements.get_features(self.user), entitlements.FREE_FEATURES)
        self.assertFalse(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))
        self.assertTrue(entitlements.has_feature(self.user, features.LIMITED_SCANS))

    def test_paid_plan_features_include_free(self):
        self._add_subscription(status="active", product_id=metadata.VERITY_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertEqual(entitlements.get_features(self.user), entitlements.PAID_FEATURES)
        self.assertTrue(entitlements.has_feature(self.user, features.UNLIMITED_SCANS))
        self.assertTrue(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))
        self.assertTrue(entitlements.has_feature(self.user, features.QUICK_REIMBURSEMENT_REQUEST))
        self.assertTrue(entitlements.has_feature(self.user, features.LIMITED_SCANS))

    def test_trialing_counts_as_paid(self):
        self._add_subscription(status="trialing", product_id=metadata.VERITY_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "paid")

    def test_canceled_subscription_is_free(self):
        self._add_subscription(status="canceled", product_id=metadata.VERITY_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertFalse(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))

    def test_cancel_at_period_end_keeps_paid_access_until_cycle_end(self):
        sub = Subscription.objects.create(
            id="sub_ent_cancel_at_period_end",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "canceled", "cancel_at_period_end": True},
        )
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Test",
        )
        price = Price.objects.create(
            id=f"price_{metadata.VERITY_PRO.stripe_id}",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        SubscriptionItem.objects.create(
            id="si_cancel_at_period_end",
            livemode=False,
            created=timezone.now(),
            subscription=sub,
            price=price,
        )
        self.user.subscription = sub
        self.user.save()
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertTrue(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))

    def test_unauthenticated_user_has_no_features(self):
        self.assertFalse(entitlements.has_feature(None, features.BANK_TRANSACTION_SYNC))


class ScanUsageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="scanner",
            email="scanner@example.com",
            password="password",
        )

    def test_scan_usage_counter_increments(self):
        self.assertEqual(entitlements.get_monthly_scan_count(self.user), 0)
        entitlements.record_scan(self.user)
        entitlements.record_scan(self.user)
        self.assertEqual(entitlements.get_monthly_scan_count(self.user), 2)

    def test_free_user_can_scan_under_limit(self):
        for _ in range(features.FREE_MONTHLY_SCAN_LIMIT - 1):
            entitlements.record_scan(self.user)
        self.assertTrue(entitlements.can_scan(self.user))

    def test_free_user_blocked_at_limit(self):
        for _ in range(features.FREE_MONTHLY_SCAN_LIMIT):
            entitlements.record_scan(self.user)
        self.assertFalse(entitlements.can_scan(self.user))


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
        self.assertEqual(entitlements.get_plan(request.user), "free")
        self.assertEqual(ctx["plan_name"], metadata.VERITY_FREE.name)

    def test_subscription_with_non_active_status_is_free(self):
        from ..context_processors import subscription_status

        self._add_subscription(status="canceled")
        ctx = subscription_status(self._request())
        self.assertFalse(ctx["is_subscribed"])
        self.assertEqual(entitlements.get_plan(self.user), "free")

    def test_pro_plan_name_is_dynamic(self):
        from ..context_processors import subscription_status

        self._add_subscription(status="active", product_id=metadata.VERITY_PRO.stripe_id)
        ctx = subscription_status(self._request())
        self.assertEqual(ctx["plan_name"], metadata.VERITY_PRO.name)
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertEqual(ctx["monthly_scan_limit"], features.PRO_SCAN_LIMIT)

    def _add_subscription(self, status="active", product_id=None):
        add_subscription(self.user, self.customer, status=status, product_id=product_id)


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
            name=meta.name,
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
        self._subscribe(metadata.VERITY_PRO, metadata.BASE_PLAN_CATEGORY)
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_PRO)
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertEqual(
            entitlements.get_monthly_scan_limit(self.user),
            metadata.VERITY_PRO.monthly_scan_limit,
        )

    def test_metered_only_subscriber_stays_free(self):
        self._subscribe(metadata.USAGE_BASED_STORAGE, metadata.STORAGE_PLAN_CATEGORY)
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_FREE)

    def test_metered_price_follows_the_plan_it_is_bought_against(self):
        storage = metadata.USAGE_BASED_STORAGE
        self.assertEqual(
            storage.price_id_for_plan(metadata.VERITY_FREE),
            storage.price_ids[metadata.VERITY_FREE.stripe_id],
        )
        self.assertEqual(
            storage.price_id_for_plan(metadata.VERITY_PRO),
            storage.price_ids[metadata.VERITY_PRO.stripe_id],
        )


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
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertTrue(metadata.has_metered_storage(self.user))

    def test_usage_storage_alone_grants_no_paid_features(self):
        self._subscribe_usage_storage()
        self.assertNotIn(metadata.VERITY_PRO.features[0], entitlements.get_features(self.user))

    def test_plan_resolves_alongside_usage_storage(self):
        add_subscription(
            self.user,
            self.customer,
            status="active",
            product_id=metadata.VERITY_PRO.stripe_id,
        )
        self.assertEqual(metadata.plan_for_user(self.user), metadata.VERITY_PRO)
        self.assertEqual(entitlements.get_plan(self.user), "paid")


class DecimalStorageUnitsTests(TestCase):
    """Storage is sold in decimal units, so 10**9 bytes is one GB.

    Binary units here would grant 7.4% more quota than advertised and report
    6.9% less usage to Stripe than was actually stored.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="units",
            email="units@example.com",
            password="password",
        )
        self.assertEqual(features.BYTES_PER_GB, 10**9)

    def test_ten_to_the_nine_bytes_is_one_gigabyte(self):
        with mock.patch.object(entitlements, "get_storage_usage_bytes", return_value=10**9):
            self.assertEqual(entitlements.get_storage_usage_gb(self.user), 1.0)

    def test_one_gigabyte_reports_as_one_thousand_megabytes(self):
        with mock.patch.object(entitlements, "get_storage_usage_bytes", return_value=10**9):
            reported_mb = ceil(entitlements.get_storage_usage_gb(self.user) * 1000)
        self.assertEqual(reported_mb, 1000)


class StorageLimitTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="storage",
            email="storage@example.com",
            password="password",
        )

    def _add_subscription_with_product(self, product_id, customer=None):
        if customer is None:
            customer = Customer.objects.create(
                id=f"cus_{product_id}", livemode=False, created=timezone.now()
            )
        product = Product.objects.create(
            id=product_id,
            livemode=False,
            active=True,
            name="Test",
        )
        price = Price.objects.create(
            id=f"price_{product_id}",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        sub = Subscription.objects.create(
            id=f"sub_storage_{product_id}",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id=f"si_{product_id}",
            livemode=False,
            created=timezone.now(),
            subscription=sub,
            price=price,
        )
        self.user.subscription = sub
        self.user.save()
        return sub

    def test_free_user_gets_free_storage_limit(self):
        self.assertEqual(entitlements.get_storage_limit(self.user), features.FREE_STORAGE_LIMIT_GB)
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, "free")

    def test_paid_user_gets_pro_storage_limit(self):
        self._add_subscription_with_product(metadata.VERITY_PRO.stripe_id)
        self.assertEqual(entitlements.get_storage_limit(self.user), features.PRO_STORAGE_LIMIT_GB)
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id,
            metadata.VERITY_PRO.stripe_id,
        )

    def test_storage_usage_counts_document_sizes(self):
        from documents.models import DocumentData

        self._add_subscription_with_product(metadata.VERITY_PRO.stripe_id)
        for i, size in enumerate((features.BYTES_PER_GB, 2 * features.BYTES_PER_GB)):
            DocumentData.objects.create(
                user=self.user,
                filepath=f"users/{self.user.pk}/doc-{size}.pdf",
                file_hash=f"hash-{i}",
                file_size=size,
            )
        self.assertEqual(entitlements.get_storage_usage_gb(self.user), 3.0)
        self.assertFalse(entitlements.is_storage_limit_exceeded(self.user))

    def test_storage_limit_exceeded_blocks_scans(self):
        from documents.models import DocumentData

        DocumentData.objects.create(
            user=self.user,
            filepath=f"users/{self.user.pk}/big.pdf",
            file_hash="x",
            file_size=2 * features.BYTES_PER_GB,
        )
        self.assertTrue(entitlements.is_storage_limit_exceeded(self.user))
        self.assertFalse(entitlements.can_scan(self.user))


class UsageBasedStorageCapTests(TestCase):
    """Subscribing to usage-based storage lifts the upload cap."""

    def setUp(self):
        self.metadata = metadata
        self.user = get_user_model().objects.create_user(
            username="metered", email="metered@example.com", password="x"
        )
        self.customer = Customer.objects.create(
            id="cus_metered",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )

    def test_capped_without_a_metered_subscription(self):
        self.assertFalse(self.metadata.has_metered_storage(self.user))
        limit_mb = entitlements.get_storage_limit(self.user) * 1024
        self.assertTrue(entitlements.can_add_storage(self.user, 1_000))
        # One byte past the free allowance is refused.
        self.assertFalse(entitlements.can_add_storage(self.user, int(limit_mb * 1024**2) + 1))

    def test_uncapped_once_subscribed(self):
        add_subscription(
            self.user,
            self.customer,
            status="active",
            product_id=self.metadata.USAGE_BASED_STORAGE.stripe_id,
        )
        self.assertTrue(self.metadata.has_metered_storage(self.user))
        # Far past any plan quota: the meter bills this, the cap does not block it.
        self.assertTrue(entitlements.can_add_storage(self.user, 50 * features.BYTES_PER_GB))
