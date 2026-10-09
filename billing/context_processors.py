"""Template context processors that inject billing state into every request."""

from datetime import date
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest
from djstripe.models import Product

from . import entitlements, metadata

BILLING_CONTEXT_CACHE_TTL = 60

_SUBSCRIPTION_STATUS_KEY = "billing:ctx:subscription:{user_id}"
_MONTHLY_USAGE_KEY = "billing:ctx:monthly:{user_id}:{period}"


def invalidate_monthly_usage_cache(user_id: int, period: str) -> None:
    cache.delete(_MONTHLY_USAGE_KEY.format(user_id=user_id, period=period))


def invalidate_subscription_status_cache(user_id: int) -> None:
    """Drop the cached subscription/plan status for one user."""
    cache.delete(_SUBSCRIPTION_STATUS_KEY.format(user_id=user_id))


def invalidate_plan_usage_caches(user_id: int) -> None:
    """Clear sidebar caches that depend on the user's active plan."""
    invalidate_subscription_status_cache(user_id)
    invalidate_monthly_usage_cache(user_id, date.today().strftime("%Y-%m"))


def _build_subscription_status(user) -> dict[str, Any]:
    held = metadata.held_products(user)
    names = Product.objects.filter(id__in=held).values_list("name", flat=True)
    return {
        "is_subscribed": bool(metadata.active_subscriptions(user)),
        "plan_name": ", ".join(sorted(n for n in names if n)) or metadata.VERITY_FREE.name,
        "monthly_scan_limit": entitlements.get_monthly_limit(user, "scan"),
    }


def subscription_status(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return _build_subscription_status(user)

    cache_key = _SUBSCRIPTION_STATUS_KEY.format(user_id=user.id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    value = _build_subscription_status(user)
    cache.set(cache_key, value, BILLING_CONTEXT_CACHE_TTL)
    return value


def monthly_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    period = date.today().strftime("%Y-%m")
    cache_key = _MONTHLY_USAGE_KEY.format(user_id=user.id, period=period)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    value = {
        "scan_usage_count": entitlements.get_monthly_count(user, "scan"),
        "upload_usage_count": entitlements.get_monthly_count(user, "upload"),
        "monthly_upload_limit": entitlements.get_monthly_limit(user, "upload"),
    }
    cache.set(cache_key, value, BILLING_CONTEXT_CACHE_TTL)
    return value
