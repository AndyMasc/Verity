from datetime import date
from typing import Any

from django.http import HttpRequest

from . import entitlements, metadata


def subscription_status(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {
            "subscription": None,
            "is_subscribed": False,
            "subscription_cancel_at_period_end": False,
            "plan": "free",
            "plan_name": metadata.PAPERTRAIL_FREE.name,
            "monthly_scan_limit": metadata.PAPERTRAIL_FREE.monthly_scan_limit,
            "features": list(entitlements.get_features(user)),
        }

    subscription = getattr(user, "subscription", None)
    is_subscribed = (
        subscription is not None
        and subscription.status in entitlements.ACTIVE_SUBSCRIPTION_STATUSES
    )

    active_products = metadata.active_products_for_user(user)
    plan_name = " + ".join(product.name for product in active_products) or (
        metadata.PAPERTRAIL_FREE.name
    )

    return {
        "subscription": subscription if is_subscribed else None,
        "is_subscribed": is_subscribed,
        "subscription_cancel_at_period_end": (
            subscription.cancel_at_period_end if subscription is not None else False
        ),
        "plan": entitlements.get_plan(user),
        "plan_name": plan_name,
        "monthly_scan_limit": entitlements.get_monthly_scan_limit(user),
        "features": list(entitlements.get_features(user)),
    }


def scan_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    monthly_scan_limit = entitlements.get_monthly_scan_limit(user)
    if monthly_scan_limit is None:
        return {}  # hide counter for unlimited users

    period = date.today().strftime("%Y-%m")
    count = entitlements.get_monthly_scan_count(user)  # already filters by this period

    return {
        "scan_usage_count": count,
        "scan_usage_period": period,
        "free_monthly_scan_limit": monthly_scan_limit,
    }


def storage_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    usage_bytes = entitlements.get_storage_usage_bytes(user)
    limit_gb = entitlements.get_storage_limit(user)

    return {
        "storage_usage_gb": usage_bytes / (1024**3),
        "storage_usage_bytes": usage_bytes,
        "storage_limit_gb": limit_gb,
        "is_storage_limit_exceeded": usage_bytes / (1024**3) >= limit_gb,
    }
