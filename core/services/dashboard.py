"""Dashboard data aggregation service.

Encapsulates the database queries and caching logic for the dashboard
view so that the view layer only handles HTTP concerns.
"""

import asyncio
from datetime import datetime, time, timedelta
from typing import cast

from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.timezone import make_aware

from core.models import Notification, UserSettings
from records.models import MergeLog, Record
from reimbursements.models import PackagePayment, ReimbursementPackage

DASHBOARD_CACHE_TTL = 60


def invalidate_dashboard_cache(user_id: int) -> None:
    cache.delete(f"dashboard:{user_id}")


async def _fetch(queryset) -> list:
    """Evaluate an async queryset into a concrete list."""
    return [row async for row in queryset]


async def _fetch_notifications(user) -> list:
    """Fetch recent unread notifications for the user."""
    return [
        n
        async for n in Notification.objects.filter(
            recipient=user,
            is_read=False,
        ).order_by("-sent_at")[:3]
    ]


async def _fetch_unread_notifications_count(user) -> int:
    """Fetch total count of unread notifications for the user."""
    return await Notification.objects.filter(
        recipient=user,
        is_read=False,
    ).acount()


# Fields the dashboard templates touch, so each stat query stays a partial load.
DASHBOARD_RECORD_FIELDS = (
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


def _convert_total(raw_items: list[tuple], to_currency: str) -> float:
    """Convert and sum a list of (amount, currency) tuples to a target currency."""
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

    active_records_qs = (
        Record.objects.visible_to(user).active().only(*DASHBOARD_RECORD_FIELDS)  # type: ignore
    )

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
        _fetch(
            active_records_qs.filter(
                transaction_date__gte=start_of_month,
                transaction_date__lte=now,
                balance__isnull=False,
            ).values_list("balance", "currency")
        ),
        _fetch(active_records_qs.order_by("-last_edited")[:5]),
        _fetch(
            active_records_qs.filter(
                expiry_date__gte=now.date(), expiry_date__lte=expiring_cutoff.date()
            ).order_by("expiry_date")
        ),
        get_webpush_warning(user),
        _fetch(
            PackagePayment.objects.filter(
                package__creator=user,
                is_completed=True,
            ).values_list("amount_paid", "payer_currency")
        ),
        sync_to_async(
            lambda: ReimbursementPackage.objects.filter(
                Q(creator=user) | Q(recipient=user),
                deleted_at__isnull=True,
            ).aggregate(
                sent_pending_count=Count(
                    "id",
                    filter=Q(
                        creator=user,
                        status__in=[
                            ReimbursementPackage.Status.OPEN,
                            ReimbursementPackage.Status.QUEUED,
                        ],
                    ),
                ),
                received_count=Count(
                    "id",
                    filter=Q(recipient=user, status=ReimbursementPackage.Status.PAID),
                ),
            )
        )(),
        _fetch(
            PackagePayment.objects.filter(
                package__recipient=user,
                is_completed=True,
            ).values_list("amount_paid", "payer_currency")
        ),
        _fetch_notifications(user),
        _fetch_unread_notifications_count(user),
    )

    # asyncio.gather unions heterogeneous coroutine results; pin the real types
    # so downstream arithmetic/subscripting stays checked.
    merge_count = cast(int, merge_count)
    monthly_expense_rows = cast(list[tuple[str, str]], monthly_expense_rows)
    recent_records = cast(list, recent_records)
    expiring_soon = cast(list, expiring_soon)
    webpush_warning = cast(str | None, webpush_warning)
    sent_payment_rows = cast(list[tuple[str, str]], sent_payment_rows)
    reimb_stats = cast(dict[str, int], reimb_stats)
    received_payment_rows = cast(list[tuple[str, str]], received_payment_rows)
    notifications = cast(list, notifications)
    unread_notifications_count = cast(int, unread_notifications_count)

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
        "records": recent_records,
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
        "webpush_warning": webpush_warning,
        "notifications": notifications,
        "unread_notifications_count": unread_notifications_count,
        "reimbursements_sent_total": sent_reimbursements_total,
        "reimbursements_sent_pending_count": reimb_stats["sent_pending_count"],
        "reimbursements_received_total": received_reimbursements_total,
        "reimbursements_received_count": reimb_stats["received_count"],
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
