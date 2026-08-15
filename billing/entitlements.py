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
        features.AUTO_TXN_CATEGORIZATION,
        features.RECORD_SHARING,
    }
)

PAID_FEATURES = FREE_FEATURES | PAID_ONLY_FEATURES

FREE_MONTHLY_SCAN_LIMIT = features.FREE_MONTHLY_SCAN_LIMIT


def get_plan(user) -> str:
    """Return 'paid' or 'free' based on the user's active base plan.

    Storage add-ons alone never unlock paid features; only a base-plan
    product (e.g. Verity Pro) does.
    """
    from .metadata import VERITY_FREE, plan_for_user

    plan = plan_for_user(user)
    return "paid" if plan.stripe_id != VERITY_FREE.stripe_id else "free"


def get_monthly_scan_limit(user) -> int | None:
    """Return the user's monthly Quick Scan allowance, or None if unlimited.

    Driven by the user's base plan metadata, so new tiers only need to set
    "monthly_scan_limit" in their product definition.
    """
    from .metadata import plan_for_user

    return plan_for_user(user).monthly_scan_limit


def get_features(user) -> frozenset[str]:
    """Return the feature names available to the user's plan."""
    return PAID_FEATURES if get_plan(user) == "paid" else FREE_FEATURES


def has_feature(user, feature: str) -> bool:
    """Return whether the user's plan includes the given feature."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return feature in get_features(user)


def get_storage_limit(user) -> int:
    """Return the user's storage limit in GB: base plan limit plus any storage add-ons."""
    from .metadata import plan_for_user, storage_addons_for_user

    limit = plan_for_user(user).storage_limit_gb
    limit += sum(addon.storage_limit_gb for addon in storage_addons_for_user(user))
    return limit


def get_storage_usage_bytes(user) -> int:
    """Return total stored bytes via the denormalized O(1) counter."""
    from .storage import get_storage_usage_bytes as _counter

    return _counter(user)


def get_storage_usage_gb(user) -> float:
    """Return total stored gigabytes (GB)."""
    return get_storage_usage_bytes(user) / (1024**3)


def is_storage_limit_exceeded(user) -> bool:
    """Check whether the user has exceeded their assigned storage limit in GB."""
    limit_gb = get_storage_limit(user)
    usage_gb = get_storage_usage_gb(user)
    return usage_gb >= limit_gb


def can_add_storage(user, additional_bytes: int) -> bool:
    """Return whether storing *additional_bytes* more would stay within the limit."""
    limit_gb = get_storage_limit(user)
    usage_bytes = get_storage_usage_bytes(user)
    return usage_bytes + additional_bytes <= limit_gb * 1024**3


def can_scan(user) -> bool:
    """Return whether the user may run another Quick Scan this month."""
    if not user.is_authenticated:
        return False

    # Storage limit block applies to both free and paid plans
    if is_storage_limit_exceeded(user):
        return False

    limit = get_monthly_scan_limit(user)
    if limit is None:
        return True

    return get_monthly_scan_count(user) < limit


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
    while True:
        usage, _ = ScanUsage.objects.get_or_create(user=user, period=period)
        # Guard against the row being deleted between get_or_create and the
        # atomic increment (e.g. by a monthly cleanup job).
        updated = ScanUsage.objects.filter(pk=usage.pk).update(count=models.F("count") + 1)
        if updated:
            return
