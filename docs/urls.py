"""URL configuration for the docs application.

All routes are under the "docs" namespace and serve static documentation pages.
"""

from django.urls import path

from . import views

app_name = "docs"
urlpatterns = [
    path("", views.how_to_use, name="how_to_use"),
    path("pricing/", views.pricing_doc, name="pricing_doc"),
    path("privacy-policy/", views.privacy_policy, name="privacy_policy"),
    path("terms-of-service/", views.terms_of_service, name="terms_of_service"),
]
