"""Per-seat billing for organisations.
Seat count is the source of truth; sync_seats is idempotent. Signals are the
fast path — django-organizations does not fire user_removed when a member row is
deleted directly, so reconcile_org_seats is what keeps the bill correct.
"""

import contextlib
import logging

import dramatiq
from django.db import transaction
from django.dispatch import receiver
from djstripe.models import Subscription
from organizations.models import Organization, OrganizationUser
from organizations.signals import user_added, user_removed
from periodiq import cron

from .models import OrgBilling

logger = logging.getLogger(__name__)

SEAT_PLAN = "org-seats"
LIVE = ("active", "trialing", "past_due")


def seat_count(org) -> int:
    return OrganizationUser.objects.filter(organization=org).count()


def seat_items(sub):
    "How many per-seat plans the user is subscribed too"
    return [
        i
        for i in sub.items.select_related("price__product").all()
        if i.price and (i.price.product.metadata or {}).get("lookup_key") == SEAT_PLAN
    ]


def billed_quantity(sub) -> int:
    items = seat_items(sub)
    return sum(i.quantity or 0 for i in items)


def live_subscription(org):
    customer_id = getattr(getattr(org, "billing", None), "customer_id", None)
    if customer_id is None:
        return None
    for sub in Subscription.objects.filter(customer_id=customer_id):
        if getattr(sub, "status", "") in LIVE and seat_items(sub):
            return sub
    return None


def has_seat_capacity(org) -> bool:
    sub = live_subscription(org)
    return sub is None or seat_count(org) < billed_quantity(sub)


def sync_seats(org) -> int | None:
    sub = live_subscription(org)
    if sub is None:
        return None
    wanted = seat_count(org)
    if billed_quantity(sub) == wanted:
        return wanted
    sub.update(
        items=[{"id": i.id, "quantity": wanted} for i in seat_items(sub)], proration="always"
    )
    logger.info("org %s: seats -> %s", org.pk, wanted)
    return wanted


def _queue_sync(sender, **_kwargs):
    """Queue a background sync_seats task for the given organization."""
    pk = getattr(sender, "pk", None)
    if pk is None:
        return

    def run():
        with contextlib.suppress(Organization.DoesNotExist):
            sync_seats(Organization.objects.get(pk=pk))

    transaction.on_commit(run)


@receiver(user_added, dispatch_uid="billing.seats.added", weak=False)
def sync_seats_on_add(sender, **_kwargs):
    _queue_sync(sender)


@receiver(user_removed, dispatch_uid="billing.seats.removed", weak=False)
def sync_seats_on_remove(sender, **_kwargs):
    _queue_sync(sender)


@dramatiq.actor(periodic=cron("17 3 * * *"), max_retries=3, time_limit=600)
def reconcile_org_seats() -> dict:
    """Correct any organisation whose billed quantity drifted from reality."""
    drifted = 0
    for billing in OrgBilling.objects.exclude(customer__isnull=True).select_related("organization"):
        org = billing.organization
        sub = live_subscription(org)
        if sub is None or billed_quantity(sub) == seat_count(org):
            continue
        logger.warning(
            "org %s: %s billed, %s actual", org.pk, billed_quantity(sub), seat_count(org)
        )
        sync_seats(org)
        drifted += 1
    return {"drifted": drifted}
