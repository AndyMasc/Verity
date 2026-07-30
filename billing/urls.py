from django.urls import path

from . import views

urlpatterns = [
    path("pricing-page/", views.pricing_page, name="pricing_page"),
]