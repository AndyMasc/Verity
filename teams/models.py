"""django-organizations owns the Organization model, so Verity hangs markers off
it instead.

PersonalOrg names the org holding a user's own subscription, which membership
counts cannot express: a solo org and a team org look identical until someone
invites a second member. SponsoredOrg records the org a user's purchase pays
for, so the members of that org run on the sponsor's plan rather than their own.
"""

from django.conf import settings
from django.db import models


class PersonalOrg(models.Model):
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="personal_for",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="personal_org",
    )

    def __str__(self) -> str:
        return f"PersonalOrg<{self.user_id}:{self.organization_id}>"


class SponsoredOrg(models.Model):
    """An org running on a plan its sponsor bought on their own account.

    The sponsorship lasts exactly as long as the sponsor holds that plan: when
    they cancel, every member of this org falls back to their personal plan.
    """

    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="sponsored_for",
    )
    sponsor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="sponsored_orgs",
    )
    product = models.ForeignKey("djstripe.Product", on_delete=models.CASCADE, to_field="id")

    def __str__(self) -> str:
        return f"SponsoredOrg<{self.organization_id} by {self.sponsor_id}>"
