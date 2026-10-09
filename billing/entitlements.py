"""Plan entitlements: which features a user is allowed to use. Only pro features are gated;
free features have no gating in the app."""

from django.db.models import F
from django.utils import timezone

from . import metadata
from .metadata import plan_for_user


def get_monthly_upload_limit(user, action: str) -> int | None:
    if action == "upload" and metadata.has_metered_storage(user):
        return None
    plan = plan_for_user(user)
    return plan.monthly_scan_limit if action == "scan" else plan.monthly_upload_limit


def get_monthly_upload_count(user, action: str) -> int:
    from .models import MonthlyUploadUsage

    period = timezone.now().strftime("%Y-%m")
    usage = MonthlyUploadUsage.objects.filter(user=user, period=period, action=action).first()
    return usage.count if usage else 0


def record_monthly_upload_usage(user, action: str) -> None:
    from .context_processors import invalidate_monthly_usage_cache
    from .models import MonthlyUploadUsage

    period = timezone.now().strftime("%Y-%m")
    MonthlyUploadUsage.objects.get_or_create(user=user, period=period, action=action)
    MonthlyUploadUsage.objects.filter(user=user, period=period, action=action).update(
        count=F("count") + 1
    )
    invalidate_monthly_usage_cache(user.pk, period)


def can_scan(user) -> bool:
    return user.is_authenticated and within_allowance(user, "scan")


def can_upload(user) -> bool:
    return user.is_authenticated and within_allowance(user, "upload")


def within_allowance(user, action: str) -> bool:
    limit = get_monthly_upload_limit(user, action)
    return limit is None or get_monthly_upload_count(user, action) < limit


def has_feature(user, lookup_key: str) -> bool:
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return lookup_key in metadata.granted_features(user)
