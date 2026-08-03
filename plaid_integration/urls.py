"""URL routing for the Plaid banking integration module.

All endpoints are namespaced under ``plaid:`` and prefixed with ``/plaid/``
in the project-level URL config.
"""

from django.urls import path

from . import views

app_name = "plaid"

urlpatterns = [
    path("exchange-token/", views.PublicTokenExchange.as_view(), name="exchange_token"),
    path("sync/", views.SyncTransactionsView.as_view(), name="sync"),
    path(
        "create-link-token/",
        views.CreateLinkTokenView.as_view(),
        name="create_link_token",
    ),
    path(
        "create-link-token/<str:item_id>/",
        views.CreateUpdateLinkTokenView.as_view(),
        name="create_update_link_token",
    ),
    path("status/", views.PlaidStatusView.as_view(), name="status"),
    path("connect/", views.plaid_connect_page, name="connect"),
    path(
        "disconnect/<str:item_id>/",
        views.DisconnectBankView.as_view(),
        name="disconnect",
    ),
    path("webhook/", views.plaid_webhook, name="webhook"),
]
