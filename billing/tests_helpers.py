"""Shared test helpers for granting users active paid subscriptions.

Views gated by ``billing.mixins.FeatureRequiredMixin`` redirect free users to
the pricing page, so tests that exercise those views need a user with a real
active Pro subscription. ``give_pro_subscription`` builds the djstripe rows
(Product / Price / SubscriptionItem / Customer) that ``metadata`` and the
entitlement layer read.
"""

from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from . import metadata


def give_pro_subscription(user) -> Subscription:
    """Attach an active Papertrail Pro subscription to ``user``.

    The customer is linked both via ``subscriber`` and the user's ``customer``
    FK so ``metadata._active_subscriptions`` and the feature gates see it.
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
        id=metadata.PAPERTRAIL_PRO.stripe_id,
        livemode=False,
        defaults={"active": True, "name": "Papertrail Pro"},
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
