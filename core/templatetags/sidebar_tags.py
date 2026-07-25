"""Custom template tags for sidebar navigation highlighting.

Provides the ``active_link`` tag which returns the appropriate CSS classes
based on whether the current request path matches a given URL pattern.
"""

from typing import Any

from django import template
from django.urls import NoReverseMatch, reverse

register = template.Library()


@register.simple_tag(takes_context=True)
def active_link(
    context: dict[str, Any], view_name: str, base_classes: str, active_classes: str
) -> str:
    """Return ``active_classes`` when the current path matches ``view_name``, else ``base_classes``.

    Handles both resolved URL names and literal paths. A literal path of ``/``
    is excluded from prefix matching to avoid the root path always being active.
    """
    request = context.get("request")
    if not request:
        return base_classes

    try:
        target_url = reverse(view_name)
    except NoReverseMatch:
        target_url = view_name

    if request.path == target_url or (request.path.startswith(target_url) and target_url != "/"):
        return active_classes

    return base_classes
