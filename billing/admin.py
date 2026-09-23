import djstripe.admin  # noqa: F401  - registers dj-stripe's admins so the override below can replace one
from django import forms
from django.contrib import admin
from djstripe.admin.admin import WebhookEndpointAdmin as DjStripeWebhookEndpointAdmin
from djstripe.admin.forms import WebhookEndpointAdminEditForm
from djstripe.models import WebhookEndpoint

from .models import CustomUser, ScanUsage


@admin.register(ScanUsage)
class ScanUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "period", "count")
    list_filter = ("period",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("count",)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ("email", "username", "is_active", "is_staff", "is_superuser")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "username")
    readonly_fields = ("date_joined", "last_login")


class WebhookEndpointEditForm(WebhookEndpointAdminEditForm):
    instance: WebhookEndpoint

    def save(self, commit: bool = False) -> WebhookEndpoint:
        if self.instance.secret or not self._stripe_data or self._stripe_data.get("secret"):
            return super().save(commit=commit)

        # Stripe only returns the signing secret when an endpoint is created, so an
        # endpoint synced from the Stripe dashboard has none stored. dj-stripe copies
        # the stored secret back onto the Stripe response, and StripeObject raises on
        # an empty string, so skip that step and leave the stored secret untouched.
        self.add_endpoint_tolerance()
        self.add_endpoint_validation_method()
        self.instance = WebhookEndpoint.sync_from_stripe_data(self._stripe_data)
        return forms.ModelForm.save(self, commit=commit)


admin.site.unregister(WebhookEndpoint)


@admin.register(WebhookEndpoint)
class WebhookEndpointAdmin(DjStripeWebhookEndpointAdmin):
    def get_form(self, request, obj=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        if obj:
            return WebhookEndpointEditForm
        return super().get_form(request, obj, *args, **kwargs)
