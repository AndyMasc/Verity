import logging
from math import ceil

import dramatiq
import stripe
from django.db.models import Q
from django.utils import timezone
from periodiq import cron

from . import features, metadata
from .models import CustomUser
from .services import _configure

logger = logging.getLogger(__name__)


@dramatiq.actor(periodic=cron("0 * * * *"), max_retries=3, time_limit=300)
def sync_storage_usage_to_stripe():
    metered_product = metadata.USAGE_BASED_STORAGE
    # Subscription.status is a property over stripe_data, so it cannot be filtered
    # in SQL. Select candidates on the queryable product id instead and let
    # has_metered_storage() decide in Python, which also covers a metered
    # subscription reached through any of a user's linked Stripe customers.
    candidate_profiles = (
        CustomUser.objects.filter(
            Q(customer__subscriptions__items__price__product_id=metered_product.stripe_id)
            | Q(subscription__items__price__product_id=metered_product.stripe_id),
            is_active=True,
            customer__isnull=False,
        )
        .distinct()
        .select_related("customer", "subscription")
    )
    _configure()

    profiles = list(candidate_profiles)
    if not profiles:
        return

    # Everything the loop needs, fetched once for the whole batch rather than per
    # user: this ran about five queries per subscriber, every hour.
    metadata.prime_active_subscriptions(profiles)
    usage_mb = {
        row[0]: ceil(row[1] / features.BYTES_PER_GB * 1000)  # Decimal GB to whole MB.
        for row in CustomUser.objects.filter(pk__in=[u.pk for u in profiles]).values_list(
            "pk", "storage_used_bytes"
        )
    }

    for profile in profiles:
        if not metadata.has_metered_storage(profile):
            continue

        # Resolved per user because a metered product carries one price per plan,
        # and skipped entirely when the product is not configured yet.
        price_id = metadata.metered_price_id(metered_product, profile)
        if price_id is None:
            continue

        subscription_item_id = next(
            (
                item.id
                for subscription in profile._pt_active_subscriptions
                for item in subscription.items.all()
                if item.price is not None and item.price.id == price_id
            ),
            None,
        )
        if not subscription_item_id:
            continue

        try:
            idempotency_key = f"storage_usage_{profile.id}_{timezone.now().strftime('%Y%m%d%H')}"

            stripe.billing.MeterEvent.create(
                event_name="storage_usage",
                payload={
                    "value": str(usage_mb.get(profile.pk, 0)),
                    "stripe_customer_id": profile.customer.id,
                },
                idempotency_key=idempotency_key,  # Retries are safe due to idempotency key being hourly unique per user
            )

        except stripe.error.StripeError as e:
            logger.error(f"Failed to sync usage for {profile.username}: {e!s}")
            raise e
