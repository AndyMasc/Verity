import logging

import dramatiq
import stripe
from django.utils import timezone
from djstripe.models import Subscription
from organizations.models import OrganizationUser
from periodiq import cron

from . import metadata
from .models import MonthlyUploadUsage, OrgBilling
from .services import _configure

logger = logging.getLogger(__name__)


def org_overage(org, action: str) -> int:
    """Members' combined overrun: the meter bills the org, counters are per member."""
    period = timezone.now().strftime("%Y-%m")
    limit = metadata.plan_for_org(org).monthly_upload_limit
    members = OrganizationUser.objects.filter(organization=org).values_list("user_id", flat=True)
    return sum(
        max(0, (count or 0) - limit)
        for count in MonthlyUploadUsage.objects.filter(
            user_id__in=members, period=period, action=action
        ).values_list("count", flat=True)
    )


@dramatiq.actor(periodic=cron("0 * * * *"), max_retries=3, time_limit=300)
def sync_upload_overage_to_stripe():
    """Report each org's upload overage to Stripe's billing meter."""
    metered_product = metadata.USAGE_BASED_STORAGE

    metered_subscriptions = (
        Subscription.objects.filter(
            items__price__product_id=metered_product.stripe_id,
            customer__org_billing__isnull=False,
        )
        .select_related("customer")
        .distinct()
    )
    org_by_customer = {
        billing.customer_id: billing.organization
        for billing in OrgBilling.objects.exclude(customer__isnull=True).select_related(
            "organization"
        )
    }

    _configure()
    for subscription in metered_subscriptions:
        # Don't meter inactive/canceled subscriptions
        if not metadata.is_active_subscription(subscription):
            continue
        org = org_by_customer[subscription.customer_id]

        overage = org_overage(org, "upload")
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
            logger.error("Failed to sync usage for org %s: %s", org.pk, e)
            raise
