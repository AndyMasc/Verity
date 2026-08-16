"""Currency conversion and platform-fee computation for reimbursements.

Pure value helpers over (amount, currency) pairs, kept out of models.py so
the data layer stays focused on fields and state.
"""

from decimal import ROUND_DOWN, Decimal

from core.currencies import to_stripe_amount
from core.exchange_rates import convert as convert_currency

STRIPE_MINIMUM_FEE_CENTS = 50
PLATFORM_FEE_PERCENT = Decimal("0.03")


class CurrencyConverter:
    """Thin wrapper around the exchange-rate service for batch conversion."""

    @staticmethod
    def convert_batch(items: list[tuple[Decimal, str]], target_currency: str) -> Decimal:
        """Convert a batch of (amount, currency) tuples to target currency."""
        from core.exchange_rates import convert_batch

        return convert_batch(items, target_currency)

    @staticmethod
    def get_active_record_items(cache: dict, records_queryset) -> list[tuple[Decimal, str]]:
        """Extract active record balance and currency pairs from cache or queryset."""
        if "records" in cache:
            return [(r.balance, r.currency) for r in cache["records"] if r.is_active and r.balance]
        return list(
            records_queryset.filter(is_active=True)
            .exclude(balance__isnull=True)
            .values_list("balance", "currency")
        )


class PlatformFeeCalculator:
    """Computes the Stripe platform (application) fee for a Connect transfer."""

    @staticmethod
    def compute(total_cents: int, payer_currency: str, rates) -> int:
        """Compute platform fee clamped to minimum and total."""
        platform_fee_cents = int(
            (Decimal(str(total_cents)) * PLATFORM_FEE_PERCENT).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
        )
        min_fee_converted = convert_currency(
            Decimal(STRIPE_MINIMUM_FEE_CENTS) / Decimal("100"),
            "usd",
            payer_currency,
            rates=rates,
        )
        min_fee_units = to_stripe_amount(min_fee_converted, payer_currency)

        if platform_fee_cents < min_fee_units:
            platform_fee_cents = min_fee_units
        if platform_fee_cents > total_cents:
            platform_fee_cents = total_cents
        return platform_fee_cents
