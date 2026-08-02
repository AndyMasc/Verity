"""Plan entitlements: which features a user is allowed to use.

A user is on the paid (Pro) plan when they hold an active or trialing
dj-stripe subscription. Paid users inherit every free feature plus the
paid-only features defined here.
"""

from django.utils import timezone

from . import features

FREE_FEATURES = frozenset(
    {
        features.LIMITED_SCANS,
        features.SUPPORTING_FILE_UPLOAD,
        features.EXPIRY_REMINDERS,
        features.REIMBURSEMENT_PAYMENTS,
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

# dj-stripe Subscription statuses that confer paid entitlements.
ACTIVE_SUBSCRIPTION_STATUSES = {"active", "trialing"}

# Monthly Quick Scan allowance for free users (LIMITED_SCANS).
FREE_MONTHLY_SCAN_LIMIT = 30


def get_plan(user) -> str:
    """Return ``"paid"`` or ``"free"`` for the given user."""
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


def can_scan(user) -> bool:
    """Return whether the user may run another Quick Scan this month."""
    if not user.is_authenticated:
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
    ScanUsage.objects.filter(pk=usage.pk).update(count=usage.count + 1)
