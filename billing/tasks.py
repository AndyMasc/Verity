import logging

import dramatiq
import stripe
from django.utils import timezone
from djstripe.models import Subscription
from periodiq import cron

from billing.entitlements import get_monthly_count, get_monthly_limit

from . import metadata
from .services import _configure

logger = logging.getLogger(__name__)


@dramatiq.actor(periodic=cron("0 * * * *"), max_retries=3, time_limit=300)
def sync_upload_overage_to_stripe():
    """Report each subscriber's upload overage to Stripe's billing meter."""
    metered_product = metadata.USAGE_BASED_STORAGE

    metered_subscriptions = (
        Subscription.objects.filter(
            items__price__product_id=metered_product.stripe_id,
            customer__subscriber__isnull=False,
        )
        .select_related("customer__subscriber")
        .distinct()
    )

    _configure()
    for subscription in metered_subscriptions:
        # Don't meter inactive/canceled subscriptions
        if not metadata.is_active_subscription(subscription):
            continue
        user = subscription.customer.subscriber

        limit = get_monthly_limit(user, "upload")
        if limit is None:
            continue

        overage = get_monthly_count(user, "upload") - limit
        if overage <= 0:
            continue
        try:
            stripe.billing.MeterEvent.create(
                event_name="document_uploads",
                payload={
                    "value": str(overage),
                    "stripe_customer_id": subscription.customer.id,
                },
                idempotency_key=f"upload_overage_{subscription.customer.id}_{timezone.now().strftime('%Y%m%d%H')}",
            )
        except stripe.error.StripeError as e:
            logger.error("Failed to sync usage for %s: %s", user.pk, e)
            raise
