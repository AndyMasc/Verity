"""Template context processors that inject webpush state into every request."""

from typing import Any

from django.core.cache import cache
from django.http import HttpRequest

WEBPUSH_STATUS_CACHE_TTL = 300


def webpush_status(request: HttpRequest) -> dict[str, Any]:
    """Add webpush subscription status to the template context.

    Returns "webpush_enabled" (bool) and "webpush_subscription_count"
    (int) so templates can conditionally show subscribe/unsubscribe UI.

    The count is cached per-user to avoid a query on every page load.
    The cache is invalidated when webpush subscriptions change (see
    "core.signals").
    """
    subscription_count = 0
    if request.user.is_authenticated:
        cache_key = f"webpush_count:{request.user.id}"
        cached = cache.get(cache_key)
        if cached is not None:
            subscription_count = cached
        else:
            subscription_count = request.user.webpush_info.count()
            cache.set(cache_key, subscription_count, WEBPUSH_STATUS_CACHE_TTL)

    return {
        "webpush_enabled": subscription_count > 0,
        "webpush_subscription_count": subscription_count,
    }
