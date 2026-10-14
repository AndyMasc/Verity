"""Give every new user a personal organisation with them as owner.

teams.0002 backfilled users who existed at migration time; this covers
everyone created since. Organization.is_active is the soft-delete flag, so an
org is never actually destroyed and the records inside it survive.
"""

from typing import Any

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify
from organizations.models import Organization, OrganizationOwner, OrganizationUser


def create_personal_org(user: Any) -> Organization:
    label = f"{user.first_name} {user.last_name}".strip() or user.email
    name = f"{label or 'user'} organisation".strip()
    slug = slugify(name)[:200] or f"user-{user.pk}"
    candidate, n = slug, 1
    while Organization.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{slug}-{n}"

    org = Organization.objects.create(name=name,
                                      slug=candidate,
                                      is_active=True)
    membership = OrganizationUser.objects.create(user=user,
                                                 organization=org,
                                                 is_admin=True)
    OrganizationOwner.objects.create(organization=org,
                                     organization_user=membership)
    return org


@receiver(post_save,
          sender=settings.AUTH_USER_MODEL,
          dispatch_uid="teams.personal_org")
def ensure_personal_org(sender: Any, instance: Any, created: bool,
                        **kwargs: Any) -> None:  # noqa: ARG001
    if not created:
        return
    if OrganizationUser.objects.filter(user=instance).exists():
        return
    create_personal_org(instance)
