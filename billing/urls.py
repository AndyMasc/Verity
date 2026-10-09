from django.urls import path

from . import views

app_name = "billing"
urlpatterns = [
    path("pricing-page/", views.pricing_page, name="pricing_page"),
    path("portal-session/", views.create_portal_session, name="portal_session"),
    path(
        "purchase_subscription/",
        views.purchase_subscription,
        name="purchase_subscription",
    ),
]
