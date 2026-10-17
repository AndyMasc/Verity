"""Org URLs. Creation and member adds are gated; the rest is stock
django-organizations. The gated paths are mounted before the stock include and
resolve to the same URLs, so every reverse() and template link is unaffected."""

from django.contrib.auth.decorators import login_required
from django.urls import include, path

from . import views

urlpatterns = [
    path(
        "add/",
        login_required(views.OrganizationCreate.as_view()),
        name="organization_add",
    ),
    path(
        "<int:organization_pk>/people/add/",
        login_required(views.OrganizationUserCreate.as_view()),
        name="organization_user_add",
    ),
    path("", include("organizations.urls")),
]
