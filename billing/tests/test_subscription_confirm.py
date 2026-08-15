from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from .helpers import FakeSession


class SubscriptionConfirmTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_existing",
            livemode=False,
            created=timezone.now(),
        )
        self.subscription = Subscription.objects.create(
            id="sub_test",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        pro_product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        pro_price = Price.objects.create(
            id="price_pro",
            livemode=False,
            active=True,
            product=pro_product,
            currency="usd",
        )
        SubscriptionItem.objects.create(
            id="si_pro",
            livemode=False,
            created=timezone.now(),
            subscription=self.subscription,
            price=pro_price,
        )
        self.user = get_user_model().objects.create_user(
            username="andy",
            email="andy@example.com",
            password="password",
        )
        self.other_user = get_user_model().objects.create_user(
            username="wendy",
            email="wendy@example.com",
            password="password",
        )
        self.url = reverse("subscription_confirm")

    def _patch_stripe(self, session):
        patchers = [
            mock.patch(
                "billing.services.retrieve_checkout_session",
                return_value=session,
            ),
            mock.patch(
                "billing.services.retrieve_subscription",
                return_value={
                    "id": "sub_test",
                    "items": {"data": [{"price": {"product": metadata.VERITY_PRO.stripe_id}}]},
                },
            ),
            mock.patch(
                "billing.views.Subscription.sync_from_stripe_data",
                return_value=self.subscription,
            ),
        ]
        for p in patchers:
            p.start()
        self.addCleanup(mock.patch.stopall)
        return self.client

    def test_stale_client_reference_id_matches_checkout_email(self):
        # The embedded pricing table reused a session whose client_reference_id
        # belongs to another user, but the email used at checkout is the
        # logged-in user's own.
        session = FakeSession(
            customer=None,
            customer_details={"email": self.user.email},
            client_reference_id=str(self.other_user.id),
        )
        client = self._patch_stripe(session)
        client.force_login(self.user)

        response = client.get(self.url, {"session_id": "cs_test"})

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.subscription_id, self.subscription.djstripe_id)
        self.assertEqual(self.user.customer_id, self.customer.djstripe_id)

    def test_matches_by_stripe_customer(self):
        self.user.customer = self.customer
        self.user.save()

        session = FakeSession(
            customer="cus_existing",
            client_reference_id=str(self.other_user.id),
        )
        client = self._patch_stripe(session)
        client.force_login(self.user)

        response = client.get(self.url, {"session_id": "cs_test"})

        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.subscription_id, self.subscription.djstripe_id)

    def test_rejects_when_no_match(self):
        session = FakeSession(
            customer="cus_someone_else",
            customer_details={"email": "someone@example.com"},
            client_reference_id=str(self.other_user.id),
        )
        client = self._patch_stripe(session)
        client.force_login(self.user)

        response = client.get(self.url, {"session_id": "cs_test"})

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.subscription_id)

    def test_rejects_unpaid_session(self):
        session = FakeSession(payment_status="unpaid")
        client = self._patch_stripe(session)
        client.force_login(self.user)

        response = client.get(self.url, {"session_id": "cs_test"})

        self.assertEqual(response.status_code, 400)
