"""Custom template tags and filters used in record-related templates.

Provides ``get_attr`` for dynamic attribute access and ``filter_url``
for building URLs that preserve existing query parameters while
updating or removing specific filter values.
"""

from typing import Any

from django import template
from django.urls import reverse

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
