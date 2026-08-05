"""Background tasks for Stripe billing reconciliation.

Local djstripe rows can drift from Stripe when a webhook delivery is missed
(Stripe drops deliveries after ~3 days of failed retries, a handler crashes,
or an endpoint is down during a deploy). This task refetches subscription
statuses from Stripe so plan entitlements never trust stale local data.
"""

import logging

from django_qstash import shared_task

from . import services

logger = logging.getLogger(__name__)


@shared_task(retries=3, backoff_factor=2)
def reconcile_subscription_statuses_task(
    subscription_ids: list[str] | None = None,
) -> int:
    """Reconcile local subscription statuses against Stripe.

    Args:
        subscription_ids: Optional list of Stripe subscription IDs to
            reconcile. When omitted, every local subscription is reconciled.

    Returns:
        Number of local rows whose stored status was corrected.
    """
    corrected = services.reconcile_subscription_statuses(subscription_ids)
    if corrected:
        logger.info("Reconciled %d subscription status(es) against Stripe.", corrected)
    return corrected
