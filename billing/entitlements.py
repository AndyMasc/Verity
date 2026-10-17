"""What a member may use, and how much of their allowance is left.

Everything resolves through the person. A feature is granted if any org they
belong to grants it, and their plan is the largest allowance among those orgs --
so joining a team someone else pays for is what raises them onto it.
"""

from django.db.models import F
from django.utils import timezone

from teams.orgs import entitled_orgs

from . import metadata


def plan_for_user(user) -> metadata.ProductMetadata:
    """The plan the user runs on: the one granting the largest allowance.

    Ties are harmless, since features are unioned separately and two plans
    allowing the same amount are indistinguishable.
    """
    if not user or not user.is_authenticated:
        return metadata.VERITY_FREE

    cached = getattr(user, "_plan", None)
    if cached is not None:
        return cached

    plans = [metadata.plan_for_org(org) for org in entitled_orgs(user)]
    user._plan = max(plans, key=lambda p: p.monthly_upload_limit, default=metadata.VERITY_FREE)
    return user._plan


def get_monthly_upload_limit(user, action: str) -> int | None:
    """None means unlimited, which usage-based storage buys."""
    if action == "upload" and has_metered_storage(user):
        return None
    plan = plan_for_user(user)
    return plan.monthly_scan_limit if action == "scan" else plan.monthly_upload_limit


def get_monthly_upload_count(user, action: str) -> int:
    from .models import MonthlyUploadUsage

    period = timezone.now().strftime("%Y-%m")
    return (
        MonthlyUploadUsage.objects.filter(user=user, period=period, action=action)
        .values_list("count", flat=True)
        .first()
        or 0
    )


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
    return within_allowance(user, "scan")


def can_upload(user) -> bool:
    return within_allowance(user, "upload")


def within_allowance(user, action: str) -> bool:
    limit = get_monthly_upload_limit(user, action)
    return limit is None or get_monthly_upload_count(user, action) < limit


def has_metered_storage(user) -> bool:
    return any(metadata.has_metered_storage(org) for org in entitled_orgs(user))


def has_feature(user, lookup_key: str) -> bool:
    if user is None or not user.is_authenticated:
        return False
    return any(lookup_key in metadata.granted_features(org) for org in entitled_orgs(user))
