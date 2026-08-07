from django.urls import path

from . import views

app_name = "reimbursements"

urlpatterns = [
    path("", views.PackageListView.as_view(), name="package-list"),
    path("onboard/", views.StripeOnboardView.as_view(), name="stripe-onboard"),
    path("validate-email/", views.validate_recipient_email, name="validate-email"),
    path("create/", views.CreatePackageFromRecordsView.as_view(), name="create-package"),
    path("success/", views.PaymentSuccessView.as_view(), name="payment-success"),
    path("<uuid:package_uuid>/", views.PackageDetailView.as_view(), name="package-detail"),
    path(
        "<uuid:package_uuid>/delete/",
        views.PackageDeleteView.as_view(),
        name="package-delete",
    ),
    path(
        "checkout/<uuid:package_uuid>/",
        views.CreatePackageCheckoutView.as_view(),
        name="create-checkout",
    ),
]
