"""URL configuration for the core application (all routes namespaced "core")."""

from django.contrib.auth.decorators import login_required
from django.urls import path

from . import views


@login_required
def trigger_error(request):
    """Raise a 500 on demand so error reporting can be verified end to end.

    Restricted to logged-in staff: an unauthenticated version lets anyone
    flood the error quota and pollute the dashboards.
    """
    if not request.user.is_staff:
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden()
    raise Exception("Intentional error triggered.")


app_name = "core"
urlpatterns = [
    path("", views.index, name="landing_page"),
    path("dashboard/", views.DashboardView.as_view(), name="dashboard"),
    path("notifications/", views.NotificationListView.as_view(), name="notifications"),
    path(
        "notifications/<int:notification_id>/delete/",
        views.notification_delete,
        name="notification-delete",
    ),
    path(
        "notifications/<int:notification_id>/read/",
        views.notification_mark_read,
        name="notification-mark-read",
    ),
    path(
        "notifications/mark-all-read/",
        views.notification_mark_all_read,
        name="notification-mark-all-read",
    ),
    path("api/expense-chart/", views.expense_chart_data, name="expense_chart_data"),
    path("privacy_policy/", views.privacy_policy, name="privacy_policy"),
    path("profile_page/", views.ProfilePageView.as_view(), name="profile_page"),
    path("health/", views.health_check, name="health_check"),
    path("trigger-error/", trigger_error),
]
