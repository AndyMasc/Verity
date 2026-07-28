"""Root URL configuration for the Papertrail project.

Routes all top-level URL patterns including admin, third-party apps,
and the module-specific URL includes. Password management endpoints
are intentionally blocked since Papertrail uses external auth.
"""

from django.conf import settings
from django.contrib import admin
from django.http import HttpResponseForbidden
from django.urls import include, path

from core.views import safe_webpush_save_info


def trigger_error(request):
    division_by_zero = 1 / 0

def forbidden_view(request, *args, **kwargs):  # noqa: ARG001
    """Return a 403 response for disabled password management endpoints."""
    return HttpResponseForbidden("Password features are disabled.")


urlpatterns = [
    # Trigger error for Sentry testing
    path('sentry-debug/', trigger_error),
    # Landing page
    path("", include("core.urls")),
    # Admin URLs
    path("admin/", admin.site.urls),
    path("qstash/webhook/", include("django_qstash.urls")),
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
    # Webpush
    path(
        "webpush/save_information", safe_webpush_save_info, name="save_webpush_info"
    ),  # Custom URL to catch webpush POST before sent to fix webpush MultipleObjectsReturned error.
    path("webpush/", include("webpush.urls")),
    path("plaid/", include("plaid_integration.urls")),
]

if settings.DEBUG:
    urlpatterns.insert(3, path("__reload__/", include("django_browser_reload.urls")))
    urlpatterns.insert(-1, path("__debug__/", include("debug_toolbar.urls")))
