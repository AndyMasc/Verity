"""Org views, gated on Stripe features.

Both gates read the requester's own plan, since under sponsorship the org holds
no subscription of its own.
"""

from organizations.views.base import BaseOrganizationCreate, BaseOrganizationUserCreate
from organizations.views.mixins import AdminRequiredMixin

from billing.entitlements import has_feature
from billing.mixins import FeatureRequiredMixin

from .forms import OrganizationCreateForm, OrganizationUserAddForm


class OrganizationCreate(FeatureRequiredMixin, BaseOrganizationCreate):
    required_feature = "create-orgs"
    form_class = OrganizationCreateForm


class OrganizationUserCreate(FeatureRequiredMixin, AdminRequiredMixin, BaseOrganizationUserCreate):
    required_feature = "add-org-members"
    form_class = OrganizationUserAddForm

    def test_func(self):
        return has_feature(self.request.user, self.required_feature)
