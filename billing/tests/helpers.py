"""Shared test helpers for the billing test suite.

Views gated by "billing.mixins.FeatureRequiredMixin" redirect free users to
the pricing page, so tests that exercise those views need an org with a real
active subscription. "give_pro_subscription" builds the djstripe rows
(Product / Price / SubscriptionItem / Customer) that "metadata" and the
entitlement layer read, attached to the user's own org.

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
from teams.orgs import billing_org

from .. import metadata
from ..models import OrgBilling


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


def attach_customer(org, customer: Customer) -> Customer:
    """Point an org's billing row at a Stripe customer."""
    billing = org.billing
    billing.customer = customer
    billing.save(update_fields=["customer"])
    return customer


def org_customer(org, suffix: str = "") -> Customer:
    customer, _ = Customer.objects.get_or_create(
        id=f"cus_test_{org.pk}{suffix}",
        livemode=False,
        defaults={"created": timezone.now()},
    )
    return attach_customer(org, customer)


def give_pro_subscription(user) -> Subscription:
    """Attach an active Plus subscription to the user's own org."""
    org = billing_org(user)
    customer = org_customer(org)

    subscription = Subscription.objects.create(
        id=f"sub_pro_{org.pk}",
        livemode=False,
        created=timezone.now(),
        customer=customer,
        stripe_data={"status": "active"},
    )
    product, _ = Product.objects.get_or_create(
        id=metadata.PLUS.stripe_id,
        livemode=False,
        defaults={
            "active": True,
            "name": "Plus",
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
            id=f"pf_{metadata.PLUS.stripe_id}_{lookup_key}",
            livemode=False,
            defaults={"product": product, "entitlement_feature": feature},
        )
    price, _ = Price.objects.get_or_create(
        id=f"price_pro_{org.pk}",
        livemode=False,
        defaults={"active": True, "product": product, "currency": "usd"},
    )
    SubscriptionItem.objects.create(
        id=f"si_pro_{org.pk}",
        livemode=False,
        created=timezone.now(),
        subscription=subscription,
        price=price,
    )
    return subscription


def give_feature(org, product_id, lookup_key):
    """Give "org" an active subscription to "product_id" carrying one feature."""
    add_subscription(org, org_customer(org), product_id=product_id)
    feature, _ = Feature.objects.get_or_create(
        id=f"feat_{lookup_key}",
        livemode=False,
        defaults={"stripe_data": {"lookup_key": lookup_key, "name": lookup_key}},
    )
    ProductFeature.objects.get_or_create(
        id=f"pf_{product_id}_{lookup_key}",
        livemode=False,
        defaults={"product": Product.objects.get(id=product_id), "entitlement_feature": feature},
    )


def add_subscription(org, customer, status="active", product_id=None):
    """Attach a djstripe Subscription to "org".

    Returns the subscription. product_id creates the Product / Price /
    SubscriptionItem rows the entitlement layer reads.
    """
    attach_customer(org, customer)
    sub = Subscription.objects.create(
        id=f"sub_{org.pk}_{status}",
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
    return sub
