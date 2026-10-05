import logging
from math import ceil

import dramatiq
import stripe
from django.utils import timezone
from djstripe.models import SubscriptionItem
from periodiq import cron

from . import metadata
from .entitlements import (
    get_storage_usage_gb,
)
from .models import CustomUser

logger = logging.getLogger(__name__)


@dramatiq.actor(periodic=cron("0 * * * *"), max_retries=3, time_limit=300)
def sync_storage_usage_to_stripe():
    metered_product = metadata.USAGE_BASED_STORAGE
    active_storage_profiles = (
        CustomUser.objects.filter(
            is_active=True,
            customer__subscriptions__items__price__product_id=metered_product.stripe_id,
        )
        .distinct()
        .select_related("customer")
    )

    for profile in active_storage_profiles:
        if not metadata.has_metered_storage(profile):
            continue

        # Match by product rather than the expected price. A plan change can
        # leave the old item in place until Stripe reconciliation completes;
        # usage must still be reported while that is pending.
        subscription_items = SubscriptionItem.objects.filter(
            subscription__customer_id=profile.customer.id,
            price__product_id=metered_product.stripe_id,
        ).select_related("subscription")
        subscription_item = next(
            (
                item
                for item in subscription_items
                if getattr(item.subscription, "status", None)
                in metadata.ACTIVE_SUBSCRIPTION_STATUSES
            ),
            None,
        )
        if subscription_item is None:
            continue

        expected_price_id = metadata.metered_price_id(metered_product, profile)
        if expected_price_id and subscription_item.price_id != expected_price_id:
            logger.warning(
                "Metered storage price for %s is %s; expected %s",
                profile.username,
                subscription_item.price_id,
                expected_price_id,
            )

        try:
            idempotency_key = f"storage_usage_{profile.id}_{timezone.now().strftime('%Y%m%d%H')}"

            stripe.billing.MeterEvent.create(
                event_name="storage_usage",
                payload={
                    # Decimal GB to decimal MB, which is the unit Stripe meters.
                    "value": str(ceil(get_storage_usage_gb(profile) * 1000)),
                    "stripe_customer_id": profile.customer.id,
                },
                idempotency_key=idempotency_key,  # Retries are safe due to idempotency key being hourly unique per user
            )

        except stripe.error.StripeError:
            logger.exception("Failed to sync usage for %s", profile.username)
            # Let Dramatiq retry the actor; the hourly idempotency key makes
            # resending an already accepted event safe.
            raise
