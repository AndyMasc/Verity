"""Expense aggregation for the dashboard expense chart.

Returns a complete, contiguous month series so the chart can render an
accurate x-axis (zero-spend months included) and flag the final month when
it is still in progress. That lets the frontend draw a solid line for
completed months and a dashed projection segment into the current month.
"""

from calendar import month_name, monthrange
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from core.exchange_rates import convert, get_rates
from records.models import Record

PERIOD_MONTHS = {"3m": 3, "6m": 6, "1y": 12, "all": None}


def get_monthly_expense_series(user, period: str = "3m") -> dict:
    """Return per-month expense totals for "user" over "period".

    Args:
        period: "3m", "6m", "1y", or "all" (default "3m").

    Returns:
        "{"months": [{"label": "Jan 24", "total": 1234.56, "is_current": false,
        "projected_total": 1234.56}, ...], "currency": "$"}"

    - "total" is the recorded spend for the month (0.0 for empty months).
    - "is_current" marks the calendar month that is still in progress.
    - "projected_total" extrapolates month-to-date spend across the full
      month for the current month (equal to "total" for completed months).
    """
    months_back = PERIOD_MONTHS.get(period)
    user_currency = getattr(user.settings, "default_currency", "usd")

    now = timezone.now()
    now_date = now.date()
    if months_back is not None:
        start = (now - timedelta(days=months_back * 30)).date()
    else:
        earliest = (
            Record.objects.active()
            .filter(user=user, balance__isnull=False)
            .order_by("transaction_date")
            .values_list("transaction_date", flat=True)
            .first()
        )
        start = earliest or (now - timedelta(days=365)).date()

    rows = list(
        Record.objects.active()
        .filter(
            user=user,
            transaction_date__gte=start,
            transaction_date__lte=now_date,
            balance__isnull=False,
        )
        .values_list("balance", "currency", "transaction_date")
    )

    rates = get_rates("USD")

    monthly: dict[str, float] = defaultdict(float)
    for balance, currency, txn_date in rows:
        month_key = txn_date.strftime("%Y-%m")
        converted = convert(balance, currency, user_currency, rates=rates)
        monthly[month_key] += float(converted)

    # Contiguous series from the first month in range through the current
    # calendar month, so gaps render as real zero months instead of being
    # skipped (which would distort the time axis).
    current_key = now.strftime("%Y-%m")
    days_in_current = monthrange(now.year, now.month)[1]
    days_elapsed = max(now.day, 1)

    months = []
    cursor = start.replace(day=1)
    while cursor <= now_date.replace(day=1):
        month_key = cursor.strftime("%Y-%m")
        total = round(monthly.get(month_key, 0.0), 2)
        is_current = month_key == current_key
        projected = total
        if is_current:
            # Month-to-date extrapolation for the still-open month.
            projected = round(total * days_in_current / days_elapsed, 2)
        months.append(
            {
                "label": f"{month_name[cursor.month][:3]} {cursor.strftime('%y')}",
                "total": total,
                "is_current": is_current,
                "projected_total": projected,
            }
        )
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return {"months": months, "currency": user_currency}
