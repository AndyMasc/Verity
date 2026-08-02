from typing import Any

from django.http import HttpRequest

from . import entitlements


def subscription_status(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {
            "subscription": None,
            "is_subscribed": False,
            "subscription_cancel_at_period_end": False,
            "plan": "free",
            "plan_name": "Free",
            "features": set(entitlements.FREE_FEATURES),
        }

    subscription = getattr(user, "subscription", None)
    is_subscribed = (
        subscription is not None
        and subscription.status in entitlements.ACTIVE_SUBSCRIPTION_STATUSES
    )
    plan = entitlements.get_plan(user)
    return {
        "subscription": subscription if is_subscribed else None,
        "is_subscribed": is_subscribed,
        "subscription_cancel_at_period_end": (
            subscription.cancel_at_period_end
            if subscription is not None
            else False
        ),
        "plan": plan,
        "plan_name": "Papertrail Pro" if plan == "paid" else "Free",
        "features": set(entitlements.get_features(user)),
    }
