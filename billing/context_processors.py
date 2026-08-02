from datetime import date
from typing import Any

from django.http import HttpRequest

from . import entitlements, features


def subscription_status(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {
            "subscription": None,
            "is_subscribed": False,
            "subscription_cancel_at_period_end": False,
            "plan": "free",
            "plan_name": "Free",
            "features": list(entitlements.FREE_FEATURES),
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
            subscription.cancel_at_period_end if subscription is not None else False
        ),
        "plan": plan,
        "plan_name": "Papertrail Pro" if plan == "paid" else "Free",
        "features": list(entitlements.get_features(user)),
    }


def scan_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    if entitlements.has_feature(user, features.UNLIMITED_SCANS):
        return {}  # hide counter for unlimited users

    period = date.today().strftime("%Y-%m")
    count = entitlements.get_monthly_scan_count(user)  # already filters by this period

    return {
        "scan_usage_count": count,
        "scan_usage_period": period,
        "free_monthly_scan_limit": entitlements.FREE_MONTHLY_SCAN_LIMIT,
    }
