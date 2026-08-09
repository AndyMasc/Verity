"""Dashboard data aggregation service.

Encapsulates the database queries and caching logic for the dashboard
view so that the view layer only handles HTTP concerns.
"""

import asyncio
from datetime import datetime, time, timedelta

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import make_aware

from core.models import Notification, UserSettings
from documents.models import DocumentData, DocumentStatus
from records.models import MergeLog, Record
from reimbursements.models import PackagePayment, ReimbursementPackage

DASHBOARD_CACHE_TTL = 10


def invalidate_dashboard_cache(user_id: int) -> None:
    cache.delete(f"dashboard:{user_id}")


async def _fetch_records(queryset) -> list:
    """Evaluate an async queryset into a concrete list."""
    return [r async for r in queryset]


async def _fetch_values_list(queryset) -> list:
    """Evaluate a values_list queryset into a list of tuples asynchronously."""
    return [row async for row in queryset]


async def _fetch_notifications(user) -> list:
    """Fetch recent unread notifications for the user."""
    return [
        n
        async for n in Notification.objects.filter(
            recipient=user,
            is_read=False,
        ).order_by("-sent_at")[:2]
    ]


async def _fetch_unread_notifications_count(user) -> int:
    """Fetch total count of unread notifications for the user."""
    return await Notification.objects.filter(
        recipient=user,
        is_read=False,
    ).acount()


def _convert_total(raw_items: list[tuple], to_currency: str) -> float:
    """Convert and sum a list of (amount, currency) tuples to a target currency."""
    if not raw_items:
        return 0.0
    from core.exchange_rates import convert_batch

    return float(convert_batch(raw_items, to_currency))


async def get_dashboard_context(user) -> dict:
    """Return aggregated dashboard statistics for a user, using cache when available."""
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

    user_settings = await sync_to_async(UserSettings.objects.get_or_create)(user=user)
    user_currency = user_settings[0].default_currency

    all_user_records = Record.objects.visible_to(user)
    active_records_qs = all_user_records.active()

    (
        merge_count,
        monthly_expense_rows,
        recent_records,
        expiring_soon,
        webpush_warning,
        sent_payment_rows,
        reimb_stats,
        received_payment_rows,
        notifications,
        unread_notifications_count,
    ) = await asyncio.gather(
        MergeLog.objects.filter(plaid_record__user=user, undone_at__isnull=True).acount(),
        _fetch_values_list(
            active_records_qs.filter(
                transaction_date__gte=start_of_month,
                transaction_date__lte=now,
                balance__isnull=False,
            ).values_list("balance", "currency")
        ),
        _fetch_records(
            active_records_qs.order_by("-last_edited").only(
                "id",
                "title",
                "merchant",
                "balance",
                "currency",
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
            )[:4]
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
                "currency",
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
        _fetch_values_list(
            PackagePayment.objects.filter(
                package__creator=user,
                is_completed=True,
            ).values_list("amount_paid", "payer_currency")
        ),
        sync_to_async(
            lambda: ReimbursementPackage.objects.filter(
                Q(creator=user) | Q(recipient=user)
            ).aggregate(
                sent_pending_count=Count(
                    "id",
                    filter=Q(creator=user, status=ReimbursementPackage.Status.OPEN),
                ),
                received_count=Count(
                    "id",
                    filter=Q(recipient=user, status=ReimbursementPackage.Status.PAID),
                ),
                has_packages=Count("id"),
            )
        )(),
        _fetch_values_list(
            PackagePayment.objects.filter(
                package__recipient=user,
                is_completed=True,
            ).values_list("amount_paid", "payer_currency")
        ),
        _fetch_notifications(user),
        _fetch_unread_notifications_count(user),
    )

    orphaned_count = (
        await DocumentData.objects.for_user(user)
        .orphaned()
        .exclude(
            status__in=[
                DocumentStatus.COMPLETED,
                DocumentStatus.PENDING_UPLOAD,
                DocumentStatus.DELETING,
            ]
        )
        .acount()
    )

    monthly_expenses_total = await sync_to_async(_convert_total)(
        [(b, c) for b, c in monthly_expense_rows if b], user_currency
    )
    sent_reimbursements_total = await sync_to_async(_convert_total)(
        [(a, c) for a, c in sent_payment_rows], user_currency
    )
    received_reimbursements_total = await sync_to_async(_convert_total)(
        [(a, c) for a, c in received_payment_rows], user_currency
    )

    records_list_url = reverse("records:view_all_records")
    expiring_soon_count = len(expiring_soon)

    context = {
        "merged_records_count": merge_count,
        "records": recent_records,
        "expiring_soon": expiring_soon,
        "expiring_soon_count": expiring_soon_count,
        "monthly_expenses": monthly_expenses_total,
        "metrics": [
            {
                "label": f"{datetime.now().strftime('%B')} Expenses",
                "value": monthly_expenses_total,
                "trailing": f"{local_date.strftime('%B')} \u2192",
                "url": f"{records_list_url}?this_month=True",
                "currency": user_currency,
            },
            {
                "label": "Expiring Soon",
                "value": expiring_soon_count,
                "subtext": "record" if expiring_soon_count == 1 else "records",
                "trailing": "View \u2192",
                "url": f"{records_list_url}?expiring_soon=True",
            },
            {
                "label": "Matched Entries",
                "value": merge_count,
                "subtext": "match" if merge_count == 1 else "matches",
                "trailing": "Review \u2192",
                "url": f"{records_list_url}?merged=True",
            },
        ],
        "orphaned_document_count": orphaned_count,
        "webpush_warning": webpush_warning,
        "notifications": notifications,
        "unread_notifications_count": unread_notifications_count,
        "reimbursements_sent_total": sent_reimbursements_total,
        "reimbursements_sent_pending_count": reimb_stats["sent_pending_count"],
        "reimbursements_received_total": received_reimbursements_total,
        "reimbursements_received_count": reimb_stats["received_count"],
        "has_packages": reimb_stats["has_packages"] > 0,
    }

    await cache.aset(cache_key, context, DASHBOARD_CACHE_TTL)
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
