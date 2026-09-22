"""Views for the docs application: static documentation pages."""

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def how_to_use(request: HttpRequest) -> HttpResponse:
    """Render the how to use documentation page."""
    return render(request, "docs/how_to_use.html")


def pricing_doc(request: HttpRequest) -> HttpResponse:
    """Render the pricing documentation page."""
    return render(request, "docs/pricing_doc.html")


def privacy_policy(request: HttpRequest) -> HttpResponse:
    """Render the privacy policy page."""
    return render(request, "docs/privacy_policy.html")


def terms_of_service(request: HttpRequest) -> HttpResponse:
    """Render the terms of service page."""
    return render(request, "docs/terms_of_service.html")
