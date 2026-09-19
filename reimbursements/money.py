"""Currency conversion and platform-fee computation for reimbursements.

Pure value helpers over (amount, currency) pairs, kept out of models.py so
the data layer stays focused on fields and state.

Platform fee model (destination charges)
----------------------------------------
Verity bills payers through destination charges, which means Stripe deducts
its own processing fees (~2.9% + $0.30 on US cards) from OUR application fee,
not from the connected account's share. A naive "3% of total" fee therefore
nets the platform roughly nothing (or a loss below ~$300 per transaction).

The calculator instead builds the application fee bottom-up:

    application_fee = estimated_stripe_fees + platform_net_margin

so the platform's NET revenue after Stripe billing is guaranteed to be at
least ``PLATFORM_NET_PERCENT x total`` (floored at ``PLATFORM_NET_MIN_USD``),
and the connected account keeps everything above that — the maximum take-home
any charge structure can offer once Stripe's costs are paid.

All constants are plain defaults; each can be overridden via Django settings
(e.g. after negotiating ic+ pricing with Stripe) using the same names.
"""

from decimal import ROUND_CEILING, Decimal

from django.conf import settings

from core.currencies import to_stripe_amount
from core.exchange_rates import convert_strict as convert_currency

# Estimated Stripe processing costs for destination charges. Defaults match
# standard US card pricing; raise STRIPE_FEE_SAFETY_PERCENT if your card mix
# skews international (surcharges there typically add 1%–1.5%).
STRIPE_FEE_PERCENT = Decimal("0.029")
STRIPE_FEE_SAFETY_PERCENT = Decimal("0.003")
STRIPE_FEE_FIXED_USD = Decimal("0.30")

# What the platform nets AFTER Stripe takes its cut: 1% of the transaction,
# floored at $0.25-equivalent so micro-payments stay worth processing.
PLATFORM_NET_PERCENT = Decimal("0.01")
PLATFORM_NET_MIN_USD = Decimal("0.25")


def _cfg(name: str, default: Decimal) -> Decimal:
    """Read a fee constant, allowing settings-level override."""
    value = getattr(settings, name, None)
    if value is None:
        return default
    return Decimal(str(value))


def _converted_units(usd_amount: Decimal, payer_currency: str, rates) -> int:
    """Convert a USD-denominated fee component into Stripe units of the payer currency.

    Uses strict conversion: during an FX outage it is correct to fail the
    checkout rather than silently collect a mispriced fee.
    """
    converted = convert_currency(usd_amount, "usd", payer_currency, rates=rates)
    return to_stripe_amount(converted, payer_currency)


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
    """Computes the Stripe application fee for a destination charge.

    Guarantees, whenever the fee is not capped at the payment total:

        fee - actual_stripe_fees >= max(net_percent x total, net_min)

    Actual Stripe fees vary slightly by card (see the safety buffer), so the
    guarantee is against the ESTIMATE; the daily reconciliation task plus the
    Stripe dashboard are the backstop for blended-rate drift.
    """

    @staticmethod
    def compute(total_cents: int, payer_currency: str, rates) -> int:
        """Compute the application fee for a ``total_cents`` payment."""
        if total_cents <= 0:
            return 0

        total = Decimal(total_cents)

        fee_percent = _cfg("STRIPE_FEE_PERCENT", STRIPE_FEE_PERCENT) + _cfg(
            "STRIPE_FEE_SAFETY_PERCENT", STRIPE_FEE_SAFETY_PERCENT
        )
        estimated_processing_units = (total * fee_percent).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        ) + _converted_units(
            _cfg("STRIPE_FEE_FIXED_USD", STRIPE_FEE_FIXED_USD),
            payer_currency,
            rates,
        )

        net_percent = _cfg("PLATFORM_NET_PERCENT", PLATFORM_NET_PERCENT)
        net_margin_units = max(
            int((total * net_percent).quantize(Decimal("1"), rounding=ROUND_CEILING)),
            _converted_units(
                _cfg("PLATFORM_NET_MIN_USD", PLATFORM_NET_MIN_USD),
                payer_currency,
                rates,
            ),
        )

        application_fee = estimated_processing_units + net_margin_units
        if application_fee > total:
            # Degenerate micro-payment: an application fee may not exceed the
            # charge amount, so take the whole thing (creator gets nothing
            # either way; Stripe fees exceed the payment).
            return total_cents
        return int(application_fee)
