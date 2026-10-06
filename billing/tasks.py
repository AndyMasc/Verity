import logging
import time
from math import ceil

import dramatiq
import stripe
from django.db.models import Q
from django.utils import timezone
from djstripe.models import Customer, Subscription
from periodiq import cron

from core import apps as core_apps

from . import features, metadata
from .models import CustomUser
from .services import _configure

logger = logging.getLogger(__name__)


@dramatiq.actor(periodic=cron("0 * * * *"), max_retries=3, time_limit=300)
def sync_storage_usage_to_stripe():
    started = time.monotonic()
    metered_product = metadata.USAGE_BASED_STORAGE
    # Subscription.status is a property over stripe_data, so it cannot be filtered
    # in SQL. Select candidates on the queryable product id instead and let
    # has_metered_storage() decide in Python, which also covers a metered
    # subscription reached through any of a user's linked Stripe customers.
    # Each branch is an IN subquery, so a user stays one row: joining through the
    # subscriptions multiplied the rows and forced a DISTINCT over every JSON column.
    metered_subscriptions = Subscription.objects.filter(
        items__price__product_id=metered_product.stripe_id
    )
    metered_customers = Customer.objects.filter(subscriptions__in=metered_subscriptions)
    candidate_profiles = CustomUser.objects.filter(
        Q(customer__in=metered_customers)
        | Q(pk__in=metered_customers.values("subscriber"))
        | Q(subscription__in=metered_subscriptions),
        is_active=True,
        customer__isnull=False,
    ).select_related("customer", "subscription")
    _configure()

    profiles = list(candidate_profiles)
    # Everything the loop needs, fetched once for the whole batch rather than per
    # user: this ran about five queries per subscriber, every hour.
    metadata.prime_active_subscriptions(profiles)
    usage_mb = {
        row[0]: ceil(row[1] / features.BYTES_PER_GB * 1000)  # Decimal GB to whole MB.
        for row in CustomUser.objects.filter(pk__in=[u.pk for u in profiles]).values_list(
            "pk", "storage_used_bytes"
        )
    }

    sent = 0
    failed = 0
    for profile in profiles:
        if not metadata.has_metered_storage(profile):
            continue

        # Resolved per user because a metered product carries one price per plan,
        # and skipped entirely when the product is not configured yet.
        price_id = metadata.metered_price_id(metered_product, profile)
        if price_id is None:
            continue

        # The meter bills the customer that holds the metered item, which can be a
        # linked customer rather than the user's own FK one.
        metered_customer_id = next(
            (
                subscription.customer_id
                for subscription in profile._pt_active_subscriptions
                for item in subscription.items.all()
                if item.price is not None and item.price.id == price_id
            ),
            None,
        )
        if not metered_customer_id:
            continue

        try:
            idempotency_key = f"storage_usage_{profile.id}_{timezone.now().strftime('%Y%m%d%H')}"

            stripe.billing.MeterEvent.create(
                event_name="storage_usage",
                payload={
                    "value": str(usage_mb.get(profile.pk, 0)),
                    "stripe_customer_id": metered_customer_id,
                },
                idempotency_key=idempotency_key,  # Retries are safe due to idempotency key being hourly unique per user
            )
            sent += 1

        # One subscriber's failure must not cost every later subscriber their usage
        # for the hour, so carry on and fail the run once the batch is done.
        except stripe.error.StripeError as e:
            logger.error(f"Failed to sync usage for {profile.username}: {e!s}")
            failed += 1

    if core_apps.posthog_client is not None:
        core_apps.posthog_client.capture(
            "storage_usage_sync_completed",
            properties={
                "candidates": len(profiles),
                "events_sent": sent,
                "events_failed": failed,
                "duration_seconds": round(time.monotonic() - started, 1),
            },
        )

    if failed:
        # Dramatiq retries the run; the idempotency keys stop a resend for the
        # subscribers that already succeeded this hour.
        raise RuntimeError(f"Storage usage sync failed for {failed} subscriber(s)")
