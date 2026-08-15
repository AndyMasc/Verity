"""Template context processors that inject billing state into every request."""

from datetime import date
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest

from . import entitlements, metadata

BILLING_CONTEXT_CACHE_TTL = 60

_SUBSCRIPTION_STATUS_KEY = "billing:ctx:subscription:{user_id}"
_SCAN_USAGE_KEY = "billing:ctx:scan_usage:{user_id}:{period}"
_STORAGE_USAGE_KEY = "billing:ctx:storage_usage:{user_id}"


def invalidate_scan_usage_cache(user_id: int, period: str) -> None:
    """Drop the cached scan counter for one user+period (see "record_scan")."""
    cache.delete(_SCAN_USAGE_KEY.format(user_id=user_id, period=period))


def invalidate_storage_usage_cache(user_id: int) -> None:
    """Drop the cached storage usage for one user (see "adjust_storage_usage")."""
    cache.delete(_STORAGE_USAGE_KEY.format(user_id=user_id))


def _build_subscription_status(user) -> dict[str, Any]:
    active_subscriptions = metadata._active_subscriptions(user)
    is_subscribed = bool(active_subscriptions)
    primary_subscription = active_subscriptions[0] if active_subscriptions else None

    active_products = metadata.active_products_for_user(user)
    plan_name = " + ".join(product.name for product in active_products) or (
        metadata.VERITY_FREE.name
    )

    # The Stripe model instance is intentionally not cached (stale serialized
    # objects in Redis); every template consumes the primitives below instead.
    return {
        "subscription": None,
        "is_subscribed": is_subscribed,
        "subscription_cancel_at_period_end": (
            primary_subscription.cancel_at_period_end if primary_subscription is not None else False
        ),
        "plan": entitlements.get_plan(user),
        "plan_name": plan_name,
        "monthly_scan_limit": entitlements.get_monthly_scan_limit(user),
        "features": list(entitlements.get_features(user)),
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


def scan_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    monthly_scan_limit = entitlements.get_monthly_scan_limit(user)
    if monthly_scan_limit is None:
        return {}  # hide counter for unlimited users

    period = date.today().strftime("%Y-%m")
    cache_key = _SCAN_USAGE_KEY.format(user_id=user.id, period=period)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    count = entitlements.get_monthly_scan_count(user)  # already filters by this period
    value = {
        "scan_usage_count": count,
        "scan_usage_period": period,
        "free_monthly_scan_limit": monthly_scan_limit,
    }
    cache.set(cache_key, value, BILLING_CONTEXT_CACHE_TTL)
    return value


def storage_usage(request: HttpRequest) -> dict[str, Any]:
    user = request.user
    if not user.is_authenticated:
        return {}

    cache_key = _STORAGE_USAGE_KEY.format(user_id=user.id)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    usage_bytes = entitlements.get_storage_usage_bytes(user)
    limit_gb = entitlements.get_storage_limit(user)
    value = {
        "storage_usage_gb": usage_bytes / (1024**3),
        "storage_usage_bytes": usage_bytes,
        "storage_limit_gb": limit_gb,
        "is_storage_limit_exceeded": usage_bytes / (1024**3) >= limit_gb,
    }
    cache.set(cache_key, value, BILLING_CONTEXT_CACHE_TTL)
    return value
