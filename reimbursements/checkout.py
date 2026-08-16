"""Stripe checkout presentation and totals for reimbursement packages.

Builds Checkout line items, converts package totals into a payer's currency,
and prepares per-record display values for the package detail pages. Logic
lives here (not on the model) so the data layer stays focused on fields and
state; "ReimbursementPackage" delegates to these functions.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from core.currencies import to_stripe_amount
from core.exchange_rates import convert as convert_currency
from core.exchange_rates import get_rates
from records.models import Record

from .money import CurrencyConverter


@dataclass
class CheckoutItems:
    """Line items and totals for a Stripe Checkout Session."""

    line_items: list[dict] = field(default_factory=list)
    total_cents: int = 0
    total_amount: Decimal = Decimal("0.00")


@dataclass
class PackageDetailItems:
    """Per-record display values for the package detail page."""

    record_items: list[dict] = field(default_factory=list)
    converted_total: Decimal = Decimal("0.00")
    original_total: Decimal = Decimal("0.00")


def converted_total(package, to_currency: str | None = None) -> Decimal:
    """Sum of the package's active record balances converted to "to_currency"."""
    target = to_currency or package.currency
    cache = getattr(package, "_prefetched_objects_cache", {})
    items = CurrencyConverter.get_active_record_items(cache, package.records)
    if not items:
        return Decimal("0.00")
    return CurrencyConverter.convert_batch(items, target)


def converted_total_cents(package, to_currency: str | None = None) -> int:
    """ "converted_total" expressed in the target currency's smallest unit."""
    target = to_currency or package.currency
    return to_stripe_amount(converted_total(package, target), target)


def build_line_items(package, payer_currency: str) -> CheckoutItems:
    """Build Stripe line items and totals for the payer's currency.

    Converts each active record balance into the payer's currency. Falls
    back to a single line item for the whole package when no individual
    record converts to a positive Stripe amount.
    """
    rates = get_rates("USD")
    line_items: list[dict] = []
    total_cents = 0
    total_amount = Decimal("0")

    for record in package.records.filter(is_active=True):
        item = _line_item_for(record, payer_currency, rates)
        if item is None:
            continue
        line_item, converted = item
        line_items.append(line_item)
        total_cents += line_item["price_data"]["unit_amount"]
        total_amount += converted

    if line_items:
        return CheckoutItems(
            line_items=line_items, total_cents=total_cents, total_amount=total_amount
        )
    return _fallback_line_item(package, payer_currency)


def _line_item_for(record, payer_currency: str, rates) -> tuple[dict, Decimal] | None:
    """Return "(line item, converted amount)" for one record, or None when it
    has no positive balance or converts to zero in the payer's currency."""
    if not record.balance or record.balance <= 0:
        return None
    converted = convert_currency(record.balance, record.currency, payer_currency, rates=rates)
    converted_stripe = to_stripe_amount(converted, payer_currency)
    if converted_stripe <= 0:
        return None

    product_data: dict = {"name": record.title or "Expense Item"}
    if getattr(record, "merchant", None):
        product_data["description"] = f"Merchant: {record.merchant}"

    line_item = {
        "price_data": {
            "currency": payer_currency,
            "product_data": product_data,
            "unit_amount": converted_stripe,
        },
        "quantity": 1,
    }
    return line_item, converted


def _fallback_line_item(package, payer_currency: str) -> CheckoutItems:
    """A single whole-package line item, or empty items when nothing is payable."""
    fallback_cents = converted_total_cents(package, payer_currency)
    if fallback_cents <= 0:
        return CheckoutItems()
    return CheckoutItems(
        line_items=[
            {
                "price_data": {
                    "currency": payer_currency,
                    "product_data": {"name": package.title or "Reimbursement"},
                    "unit_amount": fallback_cents,
                },
                "quantity": 1,
            }
        ],
        total_cents=fallback_cents,
        total_amount=converted_total(package, payer_currency),
    )


def detail_items(package, user_currency: str) -> PackageDetailItems:
    """Compute per-record display values for the package detail page.

    Compares each record's originally requested amount (from its first
    history entry) against the current balance, both converted to the
    viewer's currency.
    """
    records = list(package.records.all())
    if not records:
        return PackageDetailItems()

    rates = get_rates("USD")
    first_histories = _first_requested_histories(records)
    record_items: list[dict] = []
    converted_total = Decimal("0")
    original_total = Decimal("0")

    for record in records:
        item, converted, original = _detail_item_for(
            record, first_histories.get(record.id), package.currency, user_currency, rates
        )
        record_items.append(item)
        converted_total += converted
        original_total += original

    return PackageDetailItems(
        record_items=record_items,
        converted_total=converted_total,
        original_total=original_total,
    )


def _first_requested_histories(records: list[Record]) -> dict[int, object]:
    """Map each record id to its first history entry (the originally requested amount)."""
    HistoricalRecord = Record.history.model
    first_by_id: dict[int, object] = {}
    history = HistoricalRecord.objects.filter(id__in=[r.id for r in records]).order_by(
        "history_date"
    )
    for entry in history:
        first_by_id.setdefault(entry.id, entry)
    return first_by_id


def _detail_item_for(
    record, first_history, package_currency: str, user_currency: str, rates
) -> tuple[dict, Decimal, Decimal]:
    """Display values for one record: "(item dict, converted, original)"."""
    original_balance = first_history.balance if first_history else record.balance
    original_currency = first_history.currency if first_history else record.currency

    original_converted = convert_currency(
        original_balance, original_currency, user_currency, rates=rates
    )
    current_converted = (
        convert_currency(record.balance, record.currency, user_currency, rates=rates)
        if record.balance
        else original_converted
    )
    original_total = convert_currency(
        original_balance, original_currency, package_currency, rates=rates
    )

    item = {
        "record": record,
        "original_converted": original_converted,
        "requested_converted": current_converted,
        "converted_currency": user_currency,
        "is_inactive": not record.is_active,
    }
    return item, current_converted, original_total


def prefetch_converted_totals(packages: list, to_currency: str) -> list:
    """Precompute each package's converted display total in one pass.

    Sets "_prefetched_converted_total" on the given instances so
    "display_total" avoids per-record conversion queries on the list page.
    """
    if not packages:
        return packages
    rates = get_rates("USD")
    for package in packages:
        package._prefetched_converted_total = _converted_total_of(package, to_currency, rates)
    return packages


def _converted_total_of(package, to_currency: str, rates) -> Decimal:
    """Sum of the package's active record balances converted to "to_currency"."""
    total = Decimal("0.00")
    for record in package.records.all():
        if record.is_active and record.balance:
            total += convert_currency(record.balance, record.currency, to_currency, rates=rates)
    return total
