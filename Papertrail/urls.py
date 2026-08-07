"""Root URL configuration for the Papertrail project.

Routes all top-level URL patterns including admin, third-party apps,
and the module-specific URL includes. Password management endpoints
are intentionally blocked since Papertrail uses external auth.
"""

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponseForbidden
from django.urls import include, path
from webpush.views import ServiceWorkerView

from billing.views import subscription_confirm
from core.views import safe_webpush_save_info


def trigger_error(request):  # noqa: ARG001
    1 / 0  # noqa: B018


def forbidden_view(request, *args, **kwargs):  # noqa: ARG001
    """Return a 403 response for disabled password management endpoints."""
    return HttpResponseForbidden("Password features are disabled.")


handler403 = "Papertrail.views.handler403"


urlpatterns = [
    # Trigger error for Sentry testing
    path("sentry-debug/", trigger_error),
    # Landing page
    path("", include("core.urls")),
    # Admin URLs
    path("admin/", admin.site.urls),
    # Block password management paths completely
    path("accounts/password/change/", forbidden_view),
    path("accounts/password/set/", forbidden_view),
    path("accounts/password/reset/", forbidden_view),
    # Include allauth normally for everything else
    path("accounts/", include("allauth.urls")),
    # Local app urls
    path("documents/", include("documents.urls")),
    path("records/", include("records.urls")),
    path("accounting/", include("accounting.urls")),
    path("reimbursements/", include("reimbursements.urls")),
    path("billing/", include("billing.urls")),
    # Stripe pricing table success URL is configured at this root path
    path("subscription-confirm/", subscription_confirm, name="subscription_confirm"),
    # Stripe webhook endpoint (djstripe). The Stripe dashboard URL must include
    # the djstripe_uuid of a synced WebhookEndpoint, e.g. /stripe/webhook/<uuid>/
    path("stripe/", include("djstripe.urls", namespace="djstripe")),
    # Webpush
    path(
        "webpush/save_information", safe_webpush_save_info, name="save_webpush_info"
    ),  # Custom URL to catch webpush POST before sent to fix webpush MultipleObjectsReturned error.
    path("webpush/", include("webpush.urls")),
    # Service worker must be served from the origin root so its scope covers
    # the whole site (webpush's default /webpush/ path limits scope).
    # Registered AFTER the webpush include so reverse('service_worker')
    # resolves to the root path (Django reverse keeps the last duplicate name).
    path("service-worker.js", ServiceWorkerView.as_view(), name="service_worker"),
    path("serviceworker.js", ServiceWorkerView.as_view()),
    path("plaid/", include("plaid_integration.urls")),
]

if settings.DEBUG:
    urlpatterns.insert(3, path("__reload__/", include("django_browser_reload.urls")))
    urlpatterns.insert(-1, path("__debug__/", include("debug_toolbar.urls")))
