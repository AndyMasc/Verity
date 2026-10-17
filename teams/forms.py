"""Org creation and member adds, gated on Stripe features.

The gate is on the form as well as the view: the view decides what the user
sees, the form decides whether the row is written. Every route that creates an
org or adds a member goes through one of these two forms.

Both gates ask about the person, not the org. Under sponsorship the entitlement
belongs to whoever bought the plan, and an org carries no subscription of its own.
"""

from django import forms
from organizations.forms import OrganizationAddForm
from organizations.forms import OrganizationUserAddForm as BaseUserAddForm

from billing.entitlements import has_feature, plan_for_user

from .models import SponsoredOrg


class OrganizationCreateForm(OrganizationAddForm):
    def clean(self):
        cleaned = super().clean()
        if not has_feature(self.request.user, "create-orgs"):
            raise forms.ValidationError("Upgrade your plan to create a team organization.")
        return cleaned

    def save(self, **kwargs):
        org = super().save(**kwargs)
        # The sponsor is this request's user, not the org's owner field, which
        # OrganizationAddForm has not set from our point of view.
        SponsoredOrg.objects.create(
            organization=org,
            sponsor=self.request.user,
            product_id=plan_for_user(self.request.user).stripe_id,
        )
        return org


class OrganizationUserAddForm(BaseUserAddForm):
    def clean(self):
        cleaned = super().clean()
        if not has_feature(self.request.user, "add-org-members"):
            raise forms.ValidationError("Upgrade your plan to add members.")
        return cleaned
