"""Views for the core application: landing page, dashboard, profile, and health check.

The dashboard view delegates aggregation to ``core.services.dashboard`` and
caches the result to reduce database load on repeated visits.
"""

import json
import logging
import time as _time
from calendar import month_name
from datetime import datetime as _dt
from datetime import timedelta
from typing import Any

import posthog
from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import DatabaseError, connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView, TemplateView, UpdateView
from django_ratelimit.decorators import ratelimit
from webpush.models import SubscriptionInfo
from webpush.views import save_info

from .forms import UpdateUserSettingsForm
from .models import Notification, UserSettings
from .services.dashboard import get_dashboard_context

logger = logging.getLogger(__name__)


def index(request: HttpRequest) -> HttpResponse:
    """Redirect authenticated users to the dashboard; serve the landing page otherwise."""
    if request.user.is_authenticated:
        return redirect("core:dashboard")
    return render(request, "core/landing_page.html")


def privacy_policy(request: HttpRequest) -> HttpResponse:
    """Render the static privacy policy page."""
    return render(request, "core/privacy_policy.html")


def health_check(request: HttpRequest) -> JsonResponse:  # noqa: ARG001
    """Return service health status for database and cache connectivity.

    Returns 200 when all checks pass, 503 otherwise. Designed to be called
    by load balancers and uptime monitors.
    """
    start = _time.monotonic()
    db_ok = True
    db_ms = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ms = round((_time.monotonic() - start) * 1000, 1)
    except DatabaseError:
        db_ok = False

    redis_ok = True
    redis_ms = 0
    try:
        redis_start = _time.monotonic()
        cache.set("health_check_ping", "ok", timeout=5)
        if cache.get("health_check_ping") != "ok":
            raise ConnectionError("Cache ping failed")
        redis_ms = round((_time.monotonic() - redis_start) * 1000, 1)
    except Exception:
        redis_ok = False

    healthy = db_ok and redis_ok
    status = 200 if healthy else 503
    return JsonResponse(
        {
            "status": "healthy" if healthy else "unhealthy",
            "database": {"status": "connected" if db_ok else "disconnected", "ms": db_ms},
            "cache": {"status": "connected" if redis_ok else "disconnected", "ms": redis_ms},
            "version": getattr(settings, "APP_VERSION", "unknown"),
        },
        status=status,
    )


@require_POST
def safe_webpush_save_info(request: HttpRequest) -> HttpResponse:
    """Deduplicate webpush subscriptions before delegating to django-webpush.

    Removes any existing SubscriptionInfo with the same endpoint to prevent
    stale or duplicate entries, then forwards the request to the upstream
    ``save_info`` handler.
    """
    try:
        post_data = json.loads(request.body.decode("utf-8"))
        endpoint = post_data.get("subscription", {}).get("endpoint")

        if endpoint:
            existing_subs = SubscriptionInfo.objects.filter(endpoint=endpoint)

            if existing_subs.exists():
                existing_subs.delete()
    except json.JSONDecodeError, KeyError, ValueError:
        logger.warning("Failed to process webpush subscription info", exc_info=True)

    return save_info(request)


class DashboardView(LoginRequiredMixin, TemplateView):
    """Main dashboard displaying record summaries, expenses, and alerts.

    Aggregates data asynchronously and caches the result per user for
    ``DASHBOARD_CACHE_TTL`` seconds to keep page loads fast.
    """

    template_name = "core/dashboard.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:  # noqa: ARG002
        return async_to_sync(self._get_async)(request)

    async def _get_async(self, request: HttpRequest) -> HttpResponse:
        from django.contrib.auth import get_user_model

        user = await get_user_model().objects.select_related("settings").aget(pk=request.user.pk)
        context = await get_dashboard_context(user)
        if context.get("webpush_warning"):
            messages.warning(self.request, context["webpush_warning"])
        posthog.capture("dashboard_viewed", distinct_id=str(request.user.pk))
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

        messages.success(self.request, "Settings saved successfully.")

        if self.request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {"djangoMessages": [{"message": "Settings saved successfully.", "level": 25}]}
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
                {"djangoMessages": [{"message": "An unresolved error exists.", "level": 40}]}
            )
            return response
        return super().form_invalid(form)


PERIOD_MONTHS = {"3m": 3, "6m": 6, "1y": 12, "all": None}


@require_GET
@ratelimit(key="user", rate="30/m", method="GET", block=True)
def expense_chart_data(request: HttpRequest) -> JsonResponse:
    """Return monthly expense aggregates for the expense chart.

    Query params:
        period – ``3m``, ``6m``, ``1y``, or ``all`` (default ``3m``).

    Response:
        ``{"months": [{"label": "Jan 24", "total": 1234.56}, ...], "currency": "$"}``
    """
    from collections import defaultdict

    from core.exchange_rates import convert, get_rates
    from records.models import Record

    period = request.GET.get("period", "3m")
    months_back = PERIOD_MONTHS.get(period)
    user_currency = getattr(request.user.settings, "default_currency", "usd")

    now = timezone.now()
    if months_back is not None:
        start = now - timedelta(days=months_back * 30)
    else:
        earliest = (
            Record.objects.filter(user=request.user, balance__isnull=False)
            .order_by("transaction_date")
            .values_list("transaction_date", flat=True)
            .first()
        )
        start = earliest or (now - timedelta(days=365))

    # Fetch raw rows: one DB query, no aggregation
    rows = list(
        Record.objects.filter(
            user=request.user,
            transaction_date__gte=start.date(),
            transaction_date__lte=now.date(),
            balance__isnull=False,
        ).values_list("balance", "currency", "transaction_date")
    )

    # Pre-fetch USD-based rates once for the entire batch
    rates = get_rates("USD")

    # Group by month and convert each amount
    monthly: dict[str, float] = defaultdict(float)
    for balance, currency, txn_date in rows:
        month_key = txn_date.strftime("%Y-%m")
        converted = convert(balance, currency, user_currency, rates=rates)
        monthly[month_key] += float(converted)

    months = []
    for month_key in sorted(monthly):
        dt = _dt.strptime(month_key, "%Y-%m")
        months.append(
            {
                "label": f"{month_name[dt.month][:3]} {dt.strftime('%y')}",
                "total": round(monthly[month_key], 2),
            }
        )

    return JsonResponse({"months": months, "currency": user_currency})


class NotificationListView(LoginRequiredMixin, ListView):
    """List all notifications for the current user, newest first."""

    model = Notification
    template_name = "core/notifications.html"
    context_object_name = "notifications"
    paginate_by = 20

    def get_queryset(self):
        return Notification.objects.filter(recipient=self.request.user).order_by("-sent_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unread_count"] = Notification.objects.filter(
            recipient=self.request.user, is_read=False
        ).count()
        return context


@require_POST
def notification_delete(request: HttpRequest, notification_id: int) -> HttpResponse:
    """Delete a single notification. Only the recipient may delete."""
    notification = get_object_or_404(Notification, pk=notification_id, recipient=request.user)
    notification.delete()
    if request.headers.get("HX-Request"):
        return HttpResponse(status=200)
    return redirect("core:notifications")


@require_POST
def notification_mark_read(request: HttpRequest, notification_id: int) -> HttpResponse:
    """Toggle read/unread on a single notification."""
    notification = get_object_or_404(Notification, pk=notification_id, recipient=request.user)
    notification.is_read = not notification.is_read
    notification.save(update_fields=["is_read"])
    if request.headers.get("HX-Request"):
        from django.template.loader import render_to_string

        html = render_to_string(
            "core/partials/notification_row.html",
            {"notification": notification},
            request=request,
        )
        return HttpResponse(html)
    return redirect("core:notifications")


@require_POST
def notification_mark_all_read(request: HttpRequest) -> HttpResponse:
    """Mark all unread notifications as read."""
    count = Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    if request.headers.get("HX-Request"):
        return HttpResponse(status=200)
    messages.success(request, f"Marked {count} notification{'s' if count != 1 else ''} as read.")
    return redirect("core:notifications")
