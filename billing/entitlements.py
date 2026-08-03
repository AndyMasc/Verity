"""Plan entitlements: which features a user is allowed to use."""

from django.db import models
from django.utils import timezone

from . import features

FREE_FEATURES = frozenset(
    {
        features.LIMITED_SCANS,
        features.SUPPORTING_FILE_UPLOAD,
        features.EXPIRY_REMINDERS,
    }
)

PAID_ONLY_FEATURES = frozenset(
    {
        features.UNLIMITED_SCANS,
        features.BANK_TRANSACTION_SYNC,
        features.QUICK_REIMBURSEMENT_REQUEST,
    }
)

PAID_FEATURES = FREE_FEATURES | PAID_ONLY_FEATURES

ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}
FREE_MONTHLY_SCAN_LIMIT = 30


def get_plan(user) -> str:
    """Return 'paid' or 'free' for the given user."""
    subscription = getattr(user, "subscription", None)
    if subscription is not None and subscription.status in ACTIVE_SUBSCRIPTION_STATUSES:
        return "paid"
    return "free"


def get_features(user) -> frozenset[str]:
    """Return the feature names available to the user's plan."""
    return PAID_FEATURES if get_plan(user) == "paid" else FREE_FEATURES


def has_feature(user, feature: str) -> bool:
    """Return whether the user's plan includes the given feature."""
    if not user.is_authenticated:
        return False
    return feature in get_features(user)


def get_storage_limit(user) -> int:
    """Return the user's storage limit in GB, from their current plan's metadata."""
    from .metadata import plan_for_user

    return plan_for_user(user).storage_limit_gb


def get_storage_usage_bytes(user) -> int:
    """Return total stored bytes directly querying DocumentData."""
    from documents.models import DocumentData
    from django.db.models import Sum

    result = DocumentData.objects.filter(user=user).aggregate(total=Sum("file_size"))
    return result["total"] or 0


def get_storage_usage_gb(user) -> float:
    """Return total stored gigabytes (GB)."""
    return get_storage_usage_bytes(user) / (1024 ** 3)


def is_storage_limit_exceeded(user) -> bool:
    """Check whether the user has exceeded their assigned storage limit in GB."""
    limit_gb = get_storage_limit(user)
    usage_gb = get_storage_usage_gb(user)
    return usage_gb >= limit_gb


def can_scan(user) -> bool:
    """Return whether the user may run another Quick Scan this month."""
    if not user.is_authenticated:
        return False

    # Storage limit block applies to both free and paid plans
    if is_storage_limit_exceeded(user):
        return False

    if get_plan(user) == "paid":
        return True

    return get_monthly_scan_count(user) < FREE_MONTHLY_SCAN_LIMIT


def get_monthly_scan_count(user) -> int:
    """Return the number of Quick Scans the user has run this calendar month."""
    from .models import ScanUsage

    period = timezone.now().strftime("%Y-%m")
    usage = ScanUsage.objects.filter(user=user, period=period).first()
    return usage.count if usage else 0


def record_scan(user) -> None:
    """Increment the user's Quick Scan counter for the current month."""
    from .models import ScanUsage

    period = timezone.now().strftime("%Y-%m")
    usage, _ = ScanUsage.objects.get_or_create(user=user, period=period)
    ScanUsage.objects.filter(pk=usage.pk).update(count=models.F("count") + 1)
