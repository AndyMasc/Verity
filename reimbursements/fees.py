"""Platform-fee computation for destination charges."""

from decimal import ROUND_CEILING, Decimal

from core.currencies import to_stripe_amount
from core.exchange_rates import convert_strict as convert_currency

# Stripe's cut, estimated. Raise the safety percent for international card mixes.
STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_SAFETY_PERCENT = Decimal("0.003")
STRIPE_FEE_FIXED_USD = Decimal("0.30")

# What the platform nets after Stripe's cut.
PLATFORM_NET_PERCENT = Decimal("0.01")
PLATFORM_NET_MIN_USD = Decimal("0.25")


def converted_units(usd_amount: Decimal, payer_currency: str, rates) -> int:
    converted = convert_currency(usd_amount,
                                 "usd",
                                 payer_currency,
                                 rates=rates)
    return to_stripe_amount(converted, payer_currency)


class CurrencyConverter:

    @staticmethod
    def get_active_record_items(cache: dict,
                                records_queryset) -> list[tuple[Decimal, str]]:
        if "records" in cache:
            return [(r.balance, r.currency) for r in cache["records"]
                    if r.is_active and r.balance]
        return list(
            records_queryset.filter(is_active=True).exclude(
                balance__isnull=True).values_list("balance", "currency"))


class PlatformFeeCalculator:

    @staticmethod
    def compute(total_cents: int, payer_currency: str, rates) -> int:
        if total_cents <= 0:
            return 0

        total = Decimal(total_cents)

        fee_percent = STRIPE_FEE_PERCENT + STRIPE_FEE_SAFETY_PERCENT
        estimated_processing_units = (total * fee_percent).quantize(
            Decimal("1"), rounding=ROUND_CEILING) + converted_units(
                STRIPE_FEE_FIXED_USD,
                payer_currency,
                rates,
            )

        net_margin_units = max(
            int((total * PLATFORM_NET_PERCENT).quantize(
                Decimal("1"), rounding=ROUND_CEILING)),
            converted_units(
                PLATFORM_NET_MIN_USD,
                payer_currency,
                rates,
            ),
        )

        application_fee = estimated_processing_units + net_margin_units
        if application_fee > total:
            # An application fee cannot exceed the charge; below this size Stripe's own fees exceed the payment anyway.
            return total_cents
        return int(application_fee)
