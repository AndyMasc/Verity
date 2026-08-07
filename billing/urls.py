from django.urls import path

from . import views

urlpatterns = [
    path("pricing-page/", views.pricing_page, name="pricing_page"),
    path("portal-session/", views.create_portal_session, name="portal_session"),
    path(
        "create-checkout-session/",
        views.create_checkout_session,
        name="create_checkout_session",
    ),
]
