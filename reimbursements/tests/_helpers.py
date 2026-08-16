"""Shared factories and helpers for reimbursements tests."""

from decimal import Decimal

import stripe
from django.contrib.auth import get_user_model

from records.models import Record

from reimbursements.models import ReimbursementPackage, StripeAccount

User = get_user_model()


def _user(email="test@example.com", **kwargs):
    username = kwargs.pop("username", email.split("@")[0])
    return User.objects.create_user(
        username=username, email=email, password="testpass123", **kwargs
    )


def _package(creator, recipient=None, status="open", **kwargs):
    return ReimbursementPackage.objects.create(
        creator=creator,
        recipient=recipient,
        title=kwargs.get("title", "Test Package"),
        status=status,
        **{k: v for k, v in kwargs.items() if k not in ("title", "status")},
    )


def _record(user, balance=Decimal("25.00")):
    return Record.objects.create(
        user=user,
        title="Test Expense",
        balance=balance,
        record_type="expense",
        is_active=True,
    )


def _reconcile_session(session_id, *, bad, good):
    """Fake retrieve_checkout_session for reconciliation tests.

    Failures and successes are routed by session id rather than call order,
    so the test does not depend on queryset iteration order.
    """
    if session_id == bad:
        raise stripe.error.StripeError("boom")
    return _FakeSession(
        id=session_id,
        payment_status="paid",
        amount_total=5000,
        currency="usd",
    )


def _stripe_account(user, active=True):
    StripeAccount.objects.filter(user=user).update(
        stripe_account_id="acct_test123" if active else "",
        stripe_details_submitted=active,
        charges_enabled=active,
        payouts_enabled=active,
    )
    user.stripe_account.refresh_from_db()
    return user.stripe_account


class _FakeSession:
    """Stands in for a stripe.CheckoutSession in reconciliation tests."""

    def __init__(self, **data):
        self._data = data
        for key, value in data.items():
            setattr(self, key, value)

    def to_dict_recursive(self):
        return dict(self._data)
