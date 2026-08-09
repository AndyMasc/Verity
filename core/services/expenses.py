"""Expense aggregation for the dashboard expense chart.

Moves the query + currency-conversion pipeline out of the view so it is
cacheable, testable, and free of request/response concerns.
"""

from calendar import month_name
from collections import defaultdict
from datetime import datetime, timedelta

from django.utils import timezone

from core.exchange_rates import convert, get_rates
from records.models import Record

PERIOD_MONTHS = {"3m": 3, "6m": 6, "1y": 12, "all": None}


def get_monthly_expense_series(user, period: str = "3m") -> dict:
    """Return per-month expense totals for "user" over "period".

    Args:
        period: "3m", "6m", "1y", or "all" (default "3m").

    Returns:
        "{"months": [{"label": "Jan 24", "total": 1234.56}, ...], "currency": "$"}"
    """
    months_back = PERIOD_MONTHS.get(period)
    user_currency = getattr(user.settings, "default_currency", "usd")

    now = timezone.now()
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
            transaction_date__lte=now.date(),
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

    months = []
    for month_key in sorted(monthly):
        dt = datetime.strptime(month_key, "%Y-%m")
        months.append(
            {
                "label": f"{month_name[dt.month][:3]} {dt.strftime('%y')}",
                "total": round(monthly[month_key], 2),
            }
        )

    return {"months": months, "currency": user_currency}
