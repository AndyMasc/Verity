"""Per-seat billing behaviour: counting, syncing, drift detection."""

import itertools
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem
from django.utils import timezone
from organizations.models import Organization, OrganizationUser

from billing.models import OrgBilling
from billing.seats import (
    SEAT_PLAN,
    billed_quantity,
    has_seat_capacity,
    seat_count,
    sync_seats,
)

User = get_user_model()


def make_user(username, email=None):
    return User.objects.create_user(
        username=username, email=email or f"{username}@x.com", password="x"
    )


def make_org(name="Acme", slug="acme", billing=None):
    org = Organization.objects.create(name=name, slug=slug, is_active=True)
    if billing:
        OrgBilling.objects.create(organization=org, customer=billing)
    return org


_seq = itertools.count()


def seat_price():
    n = next(_seq)
    product = Product.objects.create(
        id=f"prod_seats_{n}",
        livemode=False,
        active=True,
        name="Seats",
        metadata={"lookup_key": SEAT_PLAN},
    )
    return Price.objects.create(
        id=f"price_seats_{n}", livemode=False, active=True, product=product, currency="usd"
    )


def make_subscription(customer, quantity):
    subscription = Subscription.objects.create(
        id=f"sub_seats_{next(_seq)}",
        livemode=False,
        created=timezone.now(),
        customer=customer,
        stripe_data={"status": "active"},
    )
    SubscriptionItem.objects.create(
        id=f"si_seats_{next(_seq)}",
        livemode=False,
        created=timezone.now(),
        subscription=subscription,
        price=seat_price(),
        stripe_data={"quantity": quantity},
    )
    return subscription


@pytest.mark.django_db
def test_seat_count_counts_membership_rows():
    org = make_org()
    a, b = make_user("a"), make_user("b")
    org.add_user(a)
    org.add_user(b)
    assert seat_count(org) == 2


@pytest.mark.django_db
def test_seat_count_ignores_other_orgs():
    mine, theirs = make_org("Mine", "mine"), make_org("Theirs", "theirs")
    mine.add_user(make_user("a"))
    mine.add_user(make_user("b"))
    theirs.add_user(make_user("c"))
    assert seat_count(mine) == 2
    assert seat_count(theirs) == 1


@pytest.mark.django_db
def test_sync_is_a_noop_without_billing_link():
    org = make_org()
    org.add_user(make_user("a"))
    assert sync_seats(org) is None


@pytest.mark.django_db
def test_sync_updates_quantity_when_it_drifts():
    n = next(_seq)
    customer = Customer.objects.create(id=f"cus_{n}", livemode=False)
    org = make_org(billing=customer)
    org.add_user(make_user("a"))
    org.add_user(make_user("b"))
    subscription = make_subscription(customer, quantity=5)
    assert billed_quantity(subscription) == 5

    with mock.patch("djstripe.models.Subscription.update") as update:
        assert sync_seats(org) == 2

    update.assert_called_once()
    assert update.call_args.kwargs["items"][0]["quantity"] == 2
    assert update.call_args.kwargs["proration"] == "always"


@pytest.mark.django_db
def test_sync_leaves_matching_quantity_alone():
    n = next(_seq)
    customer = Customer.objects.create(id=f"cus_{n}", livemode=False)
    org = make_org(billing=customer)
    org.add_user(make_user("a"))
    make_subscription(customer, quantity=1)
    # No drift: nothing should have been written.
    assert sync_seats(org) == 1


@pytest.mark.django_db
def test_capacity_allows_unbilled_orgs():
    org = make_org()
    org.add_user(make_user("a"))
    assert has_seat_capacity(org) is True


@pytest.mark.django_db
def test_capacity_blocks_past_paid_seats():
    n = next(_seq)
    customer = Customer.objects.create(id=f"cus_{n}", livemode=False)
    org = make_org(billing=customer)
    org.add_user(make_user("a"))
    make_subscription(customer, quantity=1)
    assert has_seat_capacity(org) is False


@pytest.mark.django_db
def test_duplicate_membership_rows_are_impossible():
    """unique_together means a seat count cannot double-count one person."""
    from django.db import IntegrityError, transaction

    org = make_org()
    user = make_user("solo")
    OrganizationUser.objects.create(user=user, organization=org)
    with pytest.raises(IntegrityError), transaction.atomic():
        OrganizationUser.objects.create(user=user, organization=org)


@pytest.mark.django_db
def test_price_metadata_marks_seat_plan():
    price = seat_price()
    assert price.product.metadata["lookup_key"] == SEAT_PLAN
