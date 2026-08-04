"""Service layer for Stripe reimbursements operations.

Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import logging
from typing import Any

import stripe
from djstripe.settings import djstripe_settings  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def _configure() -> None:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    """Fetch a Stripe Checkout Session. Raises StripeError on failure."""
    _configure()
    return stripe.checkout.Session.retrieve(str(session_id))


def create_checkout_session(**kwargs: Any) -> stripe.checkout.Session:
    """Create a Stripe Checkout Session with an idempotency key."""
    _configure()
    return stripe.checkout.Session.create(**kwargs)


def retrieve_stripe_account(account_id: str) -> stripe.Account:
    """Fetch a Stripe Connect account. Raises StripeError on failure."""
    _configure()
    return stripe.Account.retrieve(str(account_id))


def create_stripe_account(email: str, user_id: int) -> stripe.Account:
    """Create an Express Stripe Connect account for a user."""
    _configure()
    return stripe.Account.create(
        type="express",
        email=email,
        metadata={"user_id": str(user_id)},
    )


def create_account_link(account_id: str, refresh_url: str, return_url: str) -> stripe.AccountLink:
    """Create an account-onboarding AccountLink for the given Connect account."""
    _configure()
    return stripe.AccountLink.create(
        account=str(account_id),
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )


def retrieve_charge(charge_id: str) -> stripe.Charge:
    """Fetch a Stripe Charge. Raises StripeError on failure."""
    _configure()
    return stripe.Charge.retrieve(str(charge_id))
