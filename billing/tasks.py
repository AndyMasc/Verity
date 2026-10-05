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
            subscription__status__in=metadata.ACTIVE_SUBSCRIPTION_STATUSES,
            subscription__items__price__product_id=metered_product.stripe_id,
        )
        .distinct()
        .select_related("customer")
    )

    for profile in active_storage_profiles:
        # Resolved per user because a metered product carries one price per plan,
        # and skipped entirely when the product is not configured yet.
        price_id = metadata.metered_price_id(metered_product, profile)
        if price_id is None:
            continue

        subscription_item_id = (
            SubscriptionItem.objects.filter(
                subscription__customer_id=profile.customer.id,
                price__id=price_id,
            )
            .values_list("id", flat=True)
            .first()
        )
        if not subscription_item_id:
            continue

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

        except stripe.error.StripeError as e:
            logger.error(f"Failed to sync usage for {profile.username}: {e!s}")
