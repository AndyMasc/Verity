from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from . import entitlements, features, metadata
from .models import CustomUser


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
        sub = Subscription.objects.create(
            id=f"sub_ent_{status}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": status},
        )
        if product_id is not None:
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
            SubscriptionItem.objects.create(
                id=f"si_{product_id}",
                livemode=False,
                created=timezone.now(),
                subscription=sub,
                price=price,
            )
        self.user.subscription = sub
        self.user.save()

    def test_free_plan_features(self):
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertEqual(entitlements.get_features(self.user), entitlements.FREE_FEATURES)
        self.assertFalse(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))
        self.assertTrue(entitlements.has_feature(self.user, features.LIMITED_SCANS))

    def test_paid_plan_features_include_free(self):
        self._add_subscription(status="active", product_id=metadata.PAPERTRAIL_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertEqual(entitlements.get_features(self.user), entitlements.PAID_FEATURES)
        self.assertTrue(entitlements.has_feature(self.user, features.UNLIMITED_SCANS))
        self.assertTrue(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))
        self.assertTrue(entitlements.has_feature(self.user, features.QUICK_REIMBURSEMENT_REQUEST))
        self.assertTrue(entitlements.has_feature(self.user, features.LIMITED_SCANS))

    def test_trialing_counts_as_paid(self):
        self._add_subscription(status="trialing", product_id=metadata.PAPERTRAIL_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "paid")

    def test_canceled_subscription_is_free(self):
        self._add_subscription(status="canceled", product_id=metadata.PAPERTRAIL_PRO.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertFalse(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))

    def test_storage_addon_alone_does_not_unlock_pro_features(self):
        self._add_subscription(status="active", product_id=metadata.STORAGE_UPGRADE_25.stripe_id)
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertEqual(entitlements.get_features(self.user), entitlements.FREE_FEATURES)
        self.assertFalse(entitlements.has_feature(self.user, features.UNLIMITED_SCANS))
        self.assertFalse(entitlements.has_feature(self.user, features.BANK_TRANSACTION_SYNC))
        self.assertFalse(entitlements.has_feature(self.user, features.QUICK_REIMBURSEMENT_REQUEST))
        self.assertTrue(entitlements.has_feature(self.user, features.LIMITED_SCANS))

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
        for _ in range(entitlements.FREE_MONTHLY_SCAN_LIMIT - 1):
            entitlements.record_scan(self.user)
        self.assertTrue(entitlements.can_scan(self.user))

    def test_free_user_blocked_at_limit(self):
        for _ in range(entitlements.FREE_MONTHLY_SCAN_LIMIT):
            entitlements.record_scan(self.user)
        self.assertFalse(entitlements.can_scan(self.user))


class ContextProcessorTests(TestCase):
    def setUp(self):
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

        from .context_processors import subscription_status

        request = self._request()
        request.user = AnonymousUser()
        ctx = subscription_status(request)
        self.assertFalse(ctx["is_subscribed"])
        self.assertEqual(ctx["plan"], "free")
        self.assertEqual(ctx["plan_name"], metadata.PAPERTRAIL_FREE.name)

    def test_subscription_with_non_active_status_is_free(self):
        from .context_processors import subscription_status

        self._add_subscription(status="canceled")
        ctx = subscription_status(self._request())
        self.assertFalse(ctx["is_subscribed"])
        self.assertEqual(ctx["plan"], "free")

    def test_storage_addon_plan_name_is_not_free(self):
        from .context_processors import subscription_status

        self._add_subscription(status="active", product_id=metadata.STORAGE_UPGRADE_25.stripe_id)
        ctx = subscription_status(self._request())
        self.assertEqual(ctx["plan_name"], metadata.STORAGE_UPGRADE_25.name)
        self.assertEqual(ctx["plan"], "free")
        self.assertEqual(ctx["monthly_scan_limit"], 30)
        self.assertNotIn(features.UNLIMITED_SCANS, ctx["features"])

    def test_pro_plan_name_is_dynamic(self):
        from .context_processors import subscription_status

        self._add_subscription(status="active", product_id=metadata.PAPERTRAIL_PRO.stripe_id)
        ctx = subscription_status(self._request())
        self.assertEqual(ctx["plan_name"], metadata.PAPERTRAIL_PRO.name)
        self.assertEqual(ctx["plan"], "paid")
        self.assertIsNone(ctx["monthly_scan_limit"])

    def _add_subscription(self, status="active", product_id=None):
        sub = Subscription.objects.create(
            id=f"sub_cp_{status}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": status},
        )
        if product_id is not None:
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
            SubscriptionItem.objects.create(
                id=f"si_{product_id}",
                livemode=False,
                created=timezone.now(),
                subscription=sub,
                price=price,
            )
        self.user.subscription = sub
        self.user.save()


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
        self.assertEqual(entitlements.get_storage_limit(self.user), 1)
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, "free")

    def test_paid_user_gets_pro_storage_limit(self):
        self._add_subscription_with_product(metadata.PAPERTRAIL_PRO.stripe_id)
        self.assertEqual(entitlements.get_storage_limit(self.user), 25)
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id,
            metadata.PAPERTRAIL_PRO.stripe_id,
        )

    def test_storage_addon_alone_boosts_storage_only(self):
        self._add_subscription_with_product(metadata.STORAGE_UPGRADE_25.stripe_id)
        self.assertEqual(entitlements.get_storage_limit(self.user), 26)
        self.assertEqual(entitlements.get_plan(self.user), "free")
        self.assertEqual(metadata.plan_for_user(self.user).stripe_id, "free")

    def test_pro_plus_storage_addon_sums_storage_and_keeps_pro_features(self):
        customer = Customer.objects.create(
            id="cus_pro_addon", livemode=False, created=timezone.now()
        )
        self._add_subscription_with_product(metadata.PAPERTRAIL_PRO.stripe_id, customer=customer)
        self._add_subscription_with_product(
            metadata.STORAGE_UPGRADE_25.stripe_id, customer=customer
        )
        self.user.customer = customer
        self.user.save()
        self.assertEqual(entitlements.get_storage_limit(self.user), 50)
        self.assertEqual(entitlements.get_plan(self.user), "paid")
        self.assertEqual(
            metadata.plan_for_user(self.user).stripe_id,
            metadata.PAPERTRAIL_PRO.stripe_id,
        )

    def test_storage_usage_counts_document_sizes(self):
        from documents.models import DocumentData

        self._add_subscription_with_product(metadata.PAPERTRAIL_PRO.stripe_id)
        for i, size in enumerate((1024**3, 2 * 1024**3)):
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
            file_size=2 * 1024**3,
        )
        self.assertTrue(entitlements.is_storage_limit_exceeded(self.user))
        self.assertFalse(entitlements.can_scan(self.user))
