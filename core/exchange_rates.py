import logging
from decimal import ROUND_HALF_UP, Decimal

import httpx
from django.core.cache import cache

from core.currencies import get_currency_decimals

logger = logging.getLogger(__name__)

CACHE_KEY = "exchange_rates:v2"
CACHE_TTL = 86_400  # 24 hours
CACHE_TTL_EMPTY = 60  # Cache empty results briefly to avoid hammering API
API_BASE = "https://api.frankfurter.dev"


def _upper(code: str) -> str:
    return code.upper()


def _fetch_rates(base: str = "USD") -> dict[str, Decimal]:
    """Fetch all available rates from Frankfurter API v2. Returns {currency_code: rate}.

    v2 returns an array of {date, base, quote, rate} objects covering 201 currencies
    from 84 central banks — far more than v1 (~30 currencies from ECB alone).
    """
    url = f"{API_BASE}/v2/rates?base={base}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
            rates: dict[str, Decimal] = {}
            for entry in data:
                code = entry.get("quote", "").upper()
                rate = entry.get("rate")
                if code and rate is not None:
                    rates[code] = Decimal(str(rate))
            rates[base.upper()] = Decimal("1")
            return rates
    except Exception:
        logger.exception("Failed to fetch exchange rates from %s", url)
        return {}


def get_rates(base: str = "USD") -> dict[str, Decimal]:
    """Get exchange rates with Redis caching."""
    base = _upper(base)
    cache_key = f"{CACHE_KEY}:{base}"

    # Store/retrieve as dict[str, str] to prevent Redis JSON serialization issues
    cached = cache.get(cache_key)
    if cached is not None:
        return {code: Decimal(rate) for code, rate in cached.items()}

    raw_rates = _fetch_rates(base)
    if raw_rates:
        cache_data = {code: str(rate) for code, rate in raw_rates.items()}
        cache.set(cache_key, cache_data, CACHE_TTL)
        return raw_rates
    else:
        cache.set(cache_key, {}, CACHE_TTL_EMPTY)
        return {}


def convert(amount: Decimal, from_curr: str, to_curr: str, rates: dict[str, Decimal]) -> Decimal:
    from_curr = from_curr.upper()
    to_curr = to_curr.upper()

    if from_curr == to_curr or not amount:
        return Decimal(str(amount))

    from_rate = rates.get(from_curr)
    to_rate = rates.get(to_curr)

    if from_rate is None:
        logger.warning("No exchange rate for %s — returning amount unchanged", from_curr)
        return Decimal(str(amount))
    if to_rate is None:
        logger.warning("No exchange rate for %s — returning amount unchanged", to_curr)
        return Decimal(str(amount))
    if from_rate == 0:
        return Decimal("0")

    # Rates are relative to base (e.g., USD)
    # Target = Amount * (To_Rate / From_Rate)
    converted = Decimal(str(amount)) * (to_rate / from_rate)

    # Quantize based on target currency decimals
    decimals = get_currency_decimals(to_curr)
    quant_target = Decimal("1") if decimals == 0 else Decimal(f"0.{'0' * decimals}")

    return converted.quantize(quant_target, rounding=ROUND_HALF_UP)


def convert_batch(
    amounts_and_currencies: list[tuple],
    to_currency: str,
) -> Decimal:
    """Convert a list of (amount, from_currency) pairs to to_currency and sum."""
    rates = get_rates("USD")
    total = Decimal("0")
    for amount, from_currency in amounts_and_currencies:
        total += convert(amount, from_currency, to_currency, rates=rates)
    return total
