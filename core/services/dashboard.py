"""Dashboard data aggregation service.

Encapsulates the database queries and caching logic for the dashboard
view so that the view layer only handles HTTP concerns.
"""

import asyncio
import logging
from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.timezone import make_aware

from core.models import Notification
from documents.models import DocumentData, DocumentStatus
from records.models import MergeLog, Record
from reimbursements.models import PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)

DASHBOARD_CACHE_TTL = 30


async def _fetch_records(queryset) -> list:
    """Helper to evaluate an async queryset into a concrete list."""
    return [r async for r in queryset]


async def _fetch_notifications(user) -> list:
    """Fetch recent unread notifications for the user."""
    return [
        n
        async for n in Notification.objects.filter(
            recipient=user,
            is_read=False,
        ).order_by("-sent_at")[:3]
    ]


async def get_dashboard_context(user) -> dict:
    """Return aggregated dashboard statistics for *user*, using cache when available."""
    cache_key = f"dashboard:{user.id}"
    cached = await cache.aget(cache_key)
    if cached is not None:
        return cached

    now = timezone.now()
    local_date = timezone.localdate(now)
    start_of_month = make_aware(
        datetime.combine(local_date.replace(day=1), time.min),
        timezone=timezone.get_current_timezone(),
    )
    expiring_cutoff = now + timedelta(days=30)

    all_user_records = Record.objects.for_user(user)
    active_records_qs = all_user_records.active()

    (
        merge_count,
        monthly_expenses,
        orphaned_count,
        pending_ocr_count,
        recent_records,
        expiring_soon,
        webpush_warning,
        sent_total,
        sent_pending_count,
        received_total,
        received_count,
        has_packages,
        notifications,
    ) = await asyncio.gather(
        MergeLog.objects.filter(plaid_record__user=user, undone_at__isnull=True).acount(),
        all_user_records.filter(
            transaction_date__gte=start_of_month,
            transaction_date__lte=now,
            balance__isnull=False,
        ).aaggregate(total=Sum("balance")),
        DocumentData.objects.for_user(user)
        .orphaned()
        .exclude(
            status__in=[
                DocumentStatus.COMPLETED,
                DocumentStatus.PENDING_UPLOAD,
                DocumentStatus.DELETING,
            ]
        )
        .acount(),
        DocumentData.objects.for_user(user)
        .filter(
            did_ocr=True,
            associated_record__isnull=True,
            status__in=[
                DocumentStatus.UPLOADED,
                DocumentStatus.PROCESSING,
                DocumentStatus.COMPLETED,
                DocumentStatus.ERROR,
            ],
        )
        .acount(),
        _fetch_records(
            active_records_qs.order_by("-last_edited").only(
                "id",
                "title",
                "merchant",
                "balance",
                "expiry_date",
                "date_added",
                "last_edited",
                "user_id",
                "is_active",
                "record_type",
                "transaction_date",
                "notes",
                "nickname",
                "payment_method",
            )[:3]
        ),
        _fetch_records(
            active_records_qs.filter(
                expiry_date__gte=now.date(), expiry_date__lte=expiring_cutoff.date()
            )
            .order_by("expiry_date")
            .only(
                "id",
                "title",
                "merchant",
                "balance",
                "expiry_date",
                "date_added",
                "last_edited",
                "user_id",
                "is_active",
                "record_type",
                "transaction_date",
                "notes",
                "nickname",
                "payment_method",
            )
        ),
        get_webpush_warning(user),
        PackagePayment.objects.filter(package__paid_by=user, is_completed=True).aaggregate(
            total=Sum("amount_paid")
        ),
        ReimbursementPackage.objects.filter(
            creator=user, status=ReimbursementPackage.Status.OPEN
        ).acount(),
        PackagePayment.objects.filter(package__recipient=user, is_completed=True).aaggregate(
            total=Sum("amount_paid")
        ),
        ReimbursementPackage.objects.filter(
            recipient=user, status=ReimbursementPackage.Status.PAID
        ).acount(),
        ReimbursementPackage.objects.filter(Q(creator=user) | Q(recipient=user)).aexists(),
        _fetch_notifications(user),
    )

    context = {
        "merged_records_count": merge_count,
        "records": recent_records,
        "expiring_soon": expiring_soon,
        "expiring_soon_count": len(expiring_soon),
        "monthly_expenses": monthly_expenses.get("total") or 0,
        "orphaned_document_count": orphaned_count,
        "pending_ocr_count": pending_ocr_count,
        "webpush_warning": webpush_warning,
        "notifications": notifications,
        "reimbursements_sent_total": float(sent_total.get("total") or 0),
        "reimbursements_sent_pending_count": sent_pending_count,
        "reimbursements_received_total": float(received_total.get("total") or 0),
        "reimbursements_received_count": received_count,
        "has_packages": has_packages,
    }

    return context


async def get_webpush_warning(user) -> str | None:
    """Check if the user's webpush settings are out of sync and return a warning message."""
    from webpush.models import PushInformation

    webpush_enabled = await PushInformation.objects.filter(user=user).aexists()
    if not webpush_enabled and user.settings.enable_push_notifications:
        return "Subscribe to push messages in settings to receive push notifications."
    if webpush_enabled and not user.settings.enable_push_notifications:
        return "Enable push messages in settings to receive push notifications."
    return None
