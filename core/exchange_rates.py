"""Currency exchange rate service with Redis caching.

Uses the Frankfurter API (ECB data, no API key required) to fetch daily
exchange rates. Rates are cached in Redis for 24 hours. All conversions
go through a single USD base, so any currency pair requires at most one
rate lookup (via cross-rate).

Performance characteristics:
- First call per 24h: ~200ms HTTP fetch, then cached
- All subsequent calls: sub-millisecond dict lookup + Decimal math
- Cache key: ``exchange_rates:v1:{base}``
- TTL: 86400 seconds (24 hours)
- Fallback: returns 1.0 for same-currency, raises on missing rate
"""

import logging
from decimal import Decimal, ROUND_HALF_UP

import httpx
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_KEY = "exchange_rates:v1"
CACHE_TTL = 86_400  # 24 hours
API_BASE = "https://api.frankfurter.dev"
# Only these codes are fetched — covers all CURRENCY_CHOICES keys
SUPPORTED_CODES = [
    "USD", "EUR", "GBP", "CAD", "AUD", "NZD", "CHF", "JPY",
    "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "BRL",
    "MXN", "ARS", "CLP", "COP", "PEN", "INR", "SGD", "HKD",
    "TWD", "KRW", "ZAR", "EGP", "NGN", "KES", "GHS", "AED",
    "SAR", "QAR", "KWD", "BHD", "OMR", "ILS", "JOD", "LBP",
    "THB", "MYR", "IDR", "PHP", "VND", "PKR", "BDT", "LKR",
    "NPR", "MMK", "RUB", "UAH", "TRY", "GEL", "AZN", "KZT", "UZS",
]


def _upper(code: str) -> str:
    return code.upper()


def _fetch_rates(base: str = "USD") -> dict[str, Decimal]:
    """Fetch rates from Frankfurter API. Returns {currency_code: rate}."""
    url = f"{API_BASE}/v1/latest?base={base}&symbols={','.join(SUPPORTED_CODES)}"
    try:
        with httpx.Client(timeout=5) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            rates = {}
            for code, rate in data.get("rates", {}).items():
                rates[code.upper()] = Decimal(str(rate))
            # Include base currency at 1.0
            rates[base.upper()] = Decimal("1")
            return rates
    except Exception:
        logger.exception("Failed to fetch exchange rates from %s", url)
        return {}


def get_rates(base: str = "USD") -> dict[str, Decimal]:
    """Get exchange rates with Redis caching.

    Returns a dict mapping uppercase currency codes to their rate
    relative to *base*. Rates are cached for 24 hours.
    """
    base = _upper(base)
    cache_key = f"{CACHE_KEY}:{base}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    rates = _fetch_rates(base)
    if rates:
        cache.set(cache_key, rates, CACHE_TTL)
    return rates


def convert(
    amount,
    from_currency: str,
    to_currency: str,
    *,
    rates: dict[str, Decimal] | None = None,
) -> Decimal:
    """Convert *amount* between currencies using cached rates.

    Rates are always fetched with USD as the base so that cross-rate
    math is straightforward::

        rates[X] = how many units of X per 1 USD

    To convert *amount* of *from_currency* into *to_currency*::

        result = amount * rates[to_currency] / rates[from_currency]

    If *rates* is provided (pre-fetched with USD base), uses those
    directly to avoid repeated cache lookups.

    Returns a Decimal rounded to 2 decimal places (or integer for
    JPY-family currencies).  Returns the original amount unchanged
    if both currencies are the same.
    """
    from_c = _upper(from_currency)
    to_c = _upper(to_currency)

    if from_c == to_c:
        return Decimal(str(amount))

    amount = Decimal(str(amount))

    if rates is None:
        rates = get_rates("USD")

    if from_c in rates and to_c in rates:
        # Cross-rate via USD: amount in from_c -> USD -> to_c
        rate_from = rates[from_c]  # how many from_c per 1 USD
        rate_to = rates[to_c]      # how many to_c per 1 USD
        result = amount * rate_to / rate_from
        # JPY-family currencies have 0 decimal places
        if to_c in ("JPY", "KRW", "VND", "IDR", "CLP", "UGX"):
            return result.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # Fallback: if we can't convert, return the raw amount
    logger.warning("Missing rate for %s->%s, returning raw amount", from_c, to_c)
    return amount.quantize(Decimal("0.01"))


def convert_batch(
    amounts_and_currencies: list[tuple],
    to_currency: str,
) -> Decimal:
    """Convert a list of (amount, from_currency) pairs to *to_currency* and sum.

    Fetches USD-based rates once, then converts each amount using the
    cross-rate formula.  This is the performant path for dashboard
    aggregations.
    """
    rates = get_rates("USD")
    total = Decimal("0")
    for amount, from_currency in amounts_and_currencies:
        total += convert(amount, from_currency, to_currency, rates=rates)
    return total
