from typing import Any

from django.http import HttpRequest


def subscription_status(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {
            "subscription": None,
            "is_subscribed": False,
            "subscription_cancel_at_period_end": False,
        }

    subscription = getattr(user, "subscription", None)
    if subscription is not None:
        return {
            "subscription": subscription,
            "is_subscribed": True,
            "subscription_cancel_at_period_end": subscription.cancel_at_period_end,
        }

    return {
        "subscription": None,
        "is_subscribed": False,
        "subscription_cancel_at_period_end": False,
    }
