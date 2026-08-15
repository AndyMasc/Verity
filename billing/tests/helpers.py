"""Shared test helpers for the billing test suite.

Views gated by "billing.mixins.FeatureRequiredMixin" redirect free users to
the pricing page, so tests that exercise those views need a user with a real
active Pro subscription. "give_pro_subscription" builds the djstripe rows
(Product / Price / SubscriptionItem / Customer) that "metadata" and the
entitlement layer read.

"FakeSession" mimics the small surface of a Stripe checkout Session that the
views read, so tests can mock "stripe.checkout.Session.retrieve" without a
network call.
"""

from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata


class FakeSession:
    def __init__(
        self,
        *,
        id="cs_test",
        payment_status="paid",
        customer=None,
        customer_details=None,
        client_reference_id=None,
        subscription="sub_test",
        url="https://checkout.stripe.com/c/pay/cs_test",
    ):
        self.id = id
        self.payment_status = payment_status
        self.customer = customer
        self.customer_details = customer_details or {}
        self.client_reference_id = client_reference_id
        self.subscription = subscription
        self.url = url

    def get(self, key, default=None):
        return getattr(self, key, default)


def give_pro_subscription(user) -> Subscription:
    """Attach an active Verity Pro subscription to "user".

    The customer is linked both via "subscriber" and the user's "customer"
    FK so "metadata._active_subscriptions" and the feature gates see it.
    """
    customer = Customer.objects.create(
        id=f"cus_pro_{user.pk}",
        livemode=False,
        created=timezone.now(),
        subscriber=user,
    )
    user.customer = customer
    user.save(update_fields=["customer"])

    subscription = Subscription.objects.create(
        id=f"sub_pro_{user.pk}",
        livemode=False,
        created=timezone.now(),
        customer=customer,
        stripe_data={"status": "active"},
    )
    product, _ = Product.objects.get_or_create(
        id=metadata.VERITY_PRO.stripe_id,
        livemode=False,
        defaults={"active": True, "name": "Verity Pro"},
    )
    price, _ = Price.objects.get_or_create(
        id=f"price_pro_{user.pk}",
        livemode=False,
        defaults={"active": True, "product": product, "currency": "usd"},
    )
    SubscriptionItem.objects.create(
        id=f"si_pro_{user.pk}",
        livemode=False,
        created=timezone.now(),
        subscription=subscription,
        price=price,
    )
    return subscription
