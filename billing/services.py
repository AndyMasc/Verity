"""Service layer for Stripe billing operations.

Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import logging

import stripe
from djstripe.settings import djstripe_settings

logger = logging.getLogger(__name__)


def _configure() -> None:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY


def retrieve_subscription(subscription_id: str) -> dict:
    """Fetch the latest Stripe subscription payload for the given ID."""
    _configure()
    return stripe.Subscription.retrieve(str(subscription_id))


def cancel_subscription(subscription_id: str) -> None:
    """Cancel a Stripe subscription, logging failures for the caller."""
    _configure()
    stripe.Subscription.cancel(subscription_id)


def retrieve_customer(customer_id: str):
    """Return the Stripe customer, or None when it no longer exists."""
    _configure()
    try:
        return stripe.Customer.retrieve(customer_id)
    except stripe.error.InvalidRequestError:
        return None


def customer_missing_in_stripe(customer_id: str) -> bool:
    """Return True when a stored customer ID no longer exists in Stripe."""
    if not customer_id:
        return True
    remote = retrieve_customer(customer_id)
    if remote is None:
        return True
    return bool(getattr(remote, "deleted", False))
