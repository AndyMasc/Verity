"""Views for the core application: landing page, dashboard, profile, and health check.

The dashboard view delegates aggregation to "core.services.dashboard" and
caches the result to reduce database load on repeated visits.
"""

import json
import logging
from inspect import iscoroutine
from typing import Any

import dramatiq
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView, TemplateView, UpdateView
from django_ratelimit.decorators import ratelimit
from webpush.models import SubscriptionInfo
from webpush.views import save_info

from billing.services import pricing_context
from core.apps import posthog_client

from .forms import UpdateUserSettingsForm
from .models import Notification, UserSettings
from .services.dashboard import get_dashboard_context

logger = logging.getLogger(__name__)


def index(request: HttpRequest) -> HttpResponse:
    """Redirect authenticated users to the dashboard; serve the landing page otherwise."""
    if request.user.is_authenticated:
        return redirect("core:dashboard")

    return render(request, "core/landing_page.html", pricing_context(request.user))


def privacy_policy(_request: HttpRequest) -> HttpResponse:
    """Redirect to the static privacy policy page served by the docs app."""
    return redirect("docs:privacy_policy", permanent=True)


@require_GET
@ratelimit(key="ip", rate="60/m", method="GET", block=True)
def health_check(request: HttpRequest) -> JsonResponse:  # noqa: ARG001
    """Return service health status for database, cache, and message queue connectivity.

    Protected against DoS via IP-based rate limiting.
    """
    # Database Check
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        logger.exception("Health check failed on Database")
        db_ok = False

    # Redis Cache Check
    redis_ok = True
    try:
        cache.set("health_check_ping", "ok", timeout=5)
        if cache.get("health_check_ping") != "ok":
            raise ConnectionError("Cache ping value mismatch")
    except Exception:
        logger.exception("Health check failed on Cache")
        redis_ok = False

    # Message Queue Broker Check
    mq_broker_ok = True
    try:
        broker = dramatiq.get_broker()
        client = getattr(broker, "client", None)
        if client and callable(getattr(client, "ping", None)):
            client.ping()
    except Exception:
        logger.exception("Health check failed on Message Queue")
        mq_broker_ok = False

    healthy = db_ok and redis_ok and mq_broker_ok
    status = 200 if healthy else 503

    return JsonResponse(
        {
            "status": "healthy" if healthy else "unhealthy",
            "database": {
                "status": "connected" if db_ok else "disconnected",
            },
            "cache": {
                "status": "connected" if redis_ok else "disconnected",
            },
            "message_queue": {
                "status": "connected" if mq_broker_ok else "disconnected",
            },
            "version": getattr(settings, "APP_VERSION", "unknown"),
        },
        status=status,
    )


@require_POST
@csrf_exempt
@login_required
def safe_webpush_save_info(request: HttpRequest) -> HttpResponse:
    """Deduplicate webpush subscriptions before delegating to django-webpush.

    Removes any existing SubscriptionInfo with the same endpoint to prevent
    stale or duplicate entries, then forwards the request to the upstream
    "save_info" handler. Requires an authenticated user; the endpoint stays
    CSRF-exempt because subscriptions are registered from service-worker
    contexts that cannot carry the CSRF token.
    """
    try:
        post_data = json.loads(request.body.decode("utf-8"))
        endpoint = post_data.get("subscription", {}).get("endpoint")

        if endpoint:
            existing_subs = SubscriptionInfo.objects.filter(endpoint=endpoint)

            if existing_subs.exists():
                existing_subs.delete()
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("Failed to process webpush subscription info", exc_info=True)

    return save_info(request)


class DashboardView(LoginRequiredMixin, TemplateView):
    """Dashboard of record summaries, expenses, and alerts (cached per user)."""

    template_name = "core/dashboard.html"

    async def dispatch(  # type: ignore[override]
        self, request: HttpRequest, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        # LoginRequiredMixin reads request.user synchronously, which is not safe on the event loop.
        request.user = await request.auser()
        response = super().dispatch(request, *args, **kwargs)
        return await response if iscoroutine(response) else response

    async def get(  # type: ignore[override]
        self,
        request: HttpRequest,
        *args: Any,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> HttpResponse:
        from django.contrib.auth import get_user_model

        user = (
            await get_user_model()
            .objects.select_related("settings")
            .aget(pk=request.user.pk)
        )
        context = await get_dashboard_context(user)
        if context.get("webpush_warning") and not await request.session.aget(
            "_webpush_warning_shown"
        ):
            messages.warning(request, context["webpush_warning"])
            await request.session.aset("_webpush_warning_shown", True)
        return self.render_to_response(context)


class ProfilePageView(LoginRequiredMixin, UpdateView):
    """User settings page for toggling automation and notification preferences.

    Supports both standard form submissions and HTMX partial updates, returning
    HX-Trigger headers for client-side message rendering when appropriate.
    """

    model = UserSettings
    template_name = "core/profile_page.html"
    context_object_name = "user_settings"
    form_class = UpdateUserSettingsForm
    success_url = reverse_lazy("core:profile_page")

    def get_object(self, queryset=None) -> UserSettings:  # noqa: ARG002
        user_settings, _ = UserSettings.objects.get_or_create(user=self.request.user)
        return user_settings

    def form_valid(self, form) -> HttpResponse:
        user_settings = form.save(commit=False)
        user_settings.user = self.request.user
        user_settings.save()

        if posthog_client is not None:
            posthog_client.capture(
                "profile_updated",
                properties={"fields_changed": list(form.changed_data)},
            )

        messages.success(self.request, "Settings saved successfully.")

        if self.request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "djangoMessages": [
                        {"message": "Settings saved successfully.", "level": 25}
                    ]
                }
            )
            return response
        return super().form_valid(form)

    def form_invalid(self, form) -> HttpResponse:
        messages.error(self.request, "An unresolved error exists.")

        if self.request.headers.get("HX-Request") == "true":
            response = render(
                self.request, "core/partials/user_settings_partial.html", {"form": form}
            )
            response.status_code = 422
            response["HX-Trigger"] = json.dumps(
                {
                    "djangoMessages": [
                        {"message": "An unresolved error exists.", "level": 40}
                    ]
                }
            )
            return response
        return super().form_invalid(form)


@require_GET
@login_required
@ratelimit(key="user", rate="30/m", method="GET", block=True)
def expense_chart_data(request: HttpRequest) -> JsonResponse:
    """Return monthly expense aggregates for the expense chart.

    Query params:
        period - "3m", "6m", "1y", or "all" (default "3m").

    Response:
        {"months": [{"label": "Jan 24", "total": 1234.56}, ...], "currency": "$"}
    """
    from .services.expenses import get_monthly_expense_series

    period = request.GET.get("period", "3m")
    return JsonResponse(get_monthly_expense_series(request.user, period))


class NotificationListView(LoginRequiredMixin, ListView):
    """List all notifications for the current user, newest first."""

    model = Notification
    template_name = "core/notifications.html"
    context_object_name = "notifications"
    paginate_by = 20

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user).order_by(
            "-sent_at"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unread_count"] = Notification.objects.filter(
            recipient=self.request.user, is_read=False
        ).count()
        return context


@require_POST
@login_required
def notification_delete(request: HttpRequest, notification_id: int) -> HttpResponse:
    """Delete a single notification. Only the recipient may delete."""
    notification = get_object_or_404(
        Notification, pk=notification_id, recipient=request.user
    )
    notification.delete()
    if request.headers.get("HX-Request"):
        return HttpResponse(status=200)
    return redirect("core:notifications")


@require_POST
@login_required
def notification_mark_read(request: HttpRequest, notification_id: int) -> HttpResponse:
    """Toggle read/unread on a single notification."""
    notification = get_object_or_404(
        Notification, pk=notification_id, recipient=request.user
    )
    notification.is_read = not notification.is_read
    notification.save(update_fields=["is_read"])
    if request.headers.get("HX-Request"):
        return render(
            request,
            "core/partials/notification_row.html",
            {"notification": notification},
        )
    return redirect("core:notifications")


@require_POST
@login_required
def notification_mark_all_read(request: HttpRequest) -> HttpResponse:
    """Mark all unread notifications as read."""
    count = Notification.objects.filter(recipient=request.user, is_read=False).update(
        is_read=True
    )
    messages.success(
        request, f"Marked {count} notification{'s' if count != 1 else ''} as read."
    )
    return redirect("core:notifications")
