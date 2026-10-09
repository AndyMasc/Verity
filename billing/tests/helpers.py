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
from djstripe.models import (
    Customer,
    Feature,
    Price,
    Product,
    ProductFeature,
    Subscription,
    SubscriptionItem,
)

from .. import features, metadata


class FakeSession:
    def __init__(
        self,
        *,
        id="cs_test",
        payment_status="paid",
        status="complete",
        customer=None,
        customer_details=None,
        client_reference_id=None,
        subscription="sub_test",
        url="https://checkout.stripe.com/c/pay/cs_test",
    ):
        self.id = id
        self.payment_status = payment_status
        self.status = status
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
    FK so "metadata.active_subscriptions" and the feature gates see it.
    """
    # Reuse whatever customer is already linked. Creating a second one with
    # subscriber=user makes Customer.get_or_create(subscriber=...) ambiguous.
    customer = user.customer or Customer.objects.filter(subscriber=user).first()
    if customer is None:
        customer = Customer.objects.create(
            id=f"cus_pro_{user.pk}",
            livemode=False,
            created=timezone.now(),
            subscriber=user,
        )
    if customer.subscriber_id != user.pk:
        customer.subscriber = user
        customer.save(update_fields=["subscriber"])
    if user.customer_id != customer.pk:
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
        defaults={
            "active": True,
            "name": "Verity Pro",
            "metadata": {"category": "base_plan"},
        },
    )
    for lookup_key in (
        "record-sharing",
        "transaction-sync",
        "reimbursement-creation",
    ):
        feature, _ = Feature.objects.get_or_create(
            id=f"feat_{lookup_key}",
            livemode=False,
            defaults={"stripe_data": {"lookup_key": lookup_key, "name": lookup_key}},
        )
        ProductFeature.objects.get_or_create(
            id=f"pf_{metadata.VERITY_PRO.stripe_id}_{lookup_key}",
            livemode=False,
            defaults={"product": product, "entitlement_feature": feature},
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


def add_subscription(user, customer, status="active", product_id=None):
    """Attach a djstripe Subscription to a user, optionally with a product.

    Returns the subscription. product_id creates the Product / Price /
    SubscriptionItem rows the entitlement layer reads.
    """
    sub = Subscription.objects.create(
        id=f"sub_{user.pk}_{status}",
        livemode=False,
        created=timezone.now(),
        customer=customer,
        stripe_data={"status": status},
    )
    if product_id is not None:
        product, _ = Product.objects.get_or_create(
            id=product_id,
            livemode=False,
            defaults={"active": True, "name": "Test", "metadata": {"category": "base_plan"}},
        )
        price, _ = Price.objects.get_or_create(
            id=f"price_{product_id}",
            livemode=False,
            active=True,
            defaults={"active": True, "product": product, "currency": "usd"},
        )
        SubscriptionItem.objects.create(
            id=f"si_{product_id}",
            livemode=False,
            created=timezone.now(),
            subscription=sub,
            price=price,
        )
    customer.subscriber = user
    customer.save(update_fields=["subscriber"])
    return sub
