from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from . import entitlements


class FeatureRequiredMixin(UserPassesTestMixin):
    """Ensures the user's plan includes a given feature.

    Set ``required_feature`` on the view to one of the constants from
    ``billing.features``. Users without the feature are redirected to the
    pricing page (or given a JSON 403 for AJAX requests).
    """

    required_feature: str | None = None

    def test_func(self) -> bool:
        feature = self.required_feature
        if feature is None:
            return True
        return entitlements.has_feature(self.request.user, feature)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()

        pricing_url = reverse("pricing_page")

        if self.request.content_type and "application/json" in self.request.content_type:
            return JsonResponse(
                {
                    "error": "This feature requires the Papertrail Pro plan.",
                    "redirect_url": pricing_url,
                },
                status=403,
            )

        messages.warning(
            self.request,
            "This feature requires the Papertrail Pro plan. Upgrade to continue.",
        )
        return redirect(pricing_url)
