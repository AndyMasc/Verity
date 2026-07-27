"""Custom template tags and filters used in record-related templates.

Provides ``get_attr`` for dynamic attribute access, ``filter_url``
for building URLs that preserve existing query parameters while
updating or removing specific filter values, and ``currency_symbol``
for rendering currency-aware amount formatting.
"""

from typing import Any

from django import template
from django.urls import reverse

from core.currencies import CURRENCY_SYMBOLS
from core.currencies import format_currency as _format_currency

register = template.Library()


@register.filter
def get_attr(obj: Any, attr: str) -> str:
    """Return ``getattr(obj, attr, "")`` — a safe dynamic attribute lookup for templates."""
    return getattr(obj, attr, "")


@register.simple_tag(takes_context=True)
def filter_url(context: dict[str, Any], view_name: str, **kwargs: Any) -> str:
    """Build a URL for *view_name* that merges *kwargs* into the current query params.

    Set a param to ``None`` to remove it. Existing query parameters not
    mentioned in *kwargs* are preserved, making it easy to toggle a single
    filter without losing others.
    """
    request = context.get("request")
    if not request:
        return reverse(view_name)

    query_params = request.GET.copy()

    for key, value in kwargs.items():
        if value is None:
            query_params.pop(key, None)
        else:
            query_params[key] = value

    base_url = reverse(view_name)

    if query_params:
        return f"{base_url}?{query_params.urlencode()}"
    return base_url


@register.filter
def currency_symbol(currency_code: str) -> str:
    """Return the display symbol for a currency code (e.g. 'usd' → '$')."""
    return CURRENCY_SYMBOLS.get(str(currency_code).lower(), str(currency_code).upper() + " ")


@register.filter
def currency_format(amount, currency_code: str) -> str:
    """Format a monetary amount with the correct currency symbol.

    Usage: ``{{ record.balance|currency_format:record.currency }}``
    """
    return _format_currency(amount, str(currency_code).lower())


@register.simple_tag(takes_context=True)
def currency_amount(context: dict[str, Any], amount, currency_code: str) -> str:  # noqa: ARG001
    """Same as ``currency_format`` but as a simple_tag for use with ``as`` syntax.

    Usage: ``{% currency_amount record.balance record.currency as val %}``
    """
    return _format_currency(amount, str(currency_code).lower())
