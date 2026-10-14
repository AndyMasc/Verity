"""Personal organisations and per-org permissions.

Every user gets a personal organisation at signup: it is the billing subject
for a solo account and what Free/Plus attach to. Team orgs are the ones you
create or are invited to.
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

    org = Organization.objects.create(name=name, slug=candidate, is_active=True)
    membership = OrganizationUser.objects.create(user=user, organization=org, is_admin=True)
    OrganizationOwner.objects.create(organization=org, organization_user=membership)
    return org


def personal_org(user: Any) -> Organization | None:
    """The org a user's own subscription belongs to.

    The personal org is the one they own alone. Team orgs are billing subjects
    in their own right, so they are skipped.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    for org in Organization.objects.filter(owner__organization_user__user=user):
        if org.organization_users.count() == 1:
            return org
    return None


def is_owner(user: Any, org: Organization) -> bool:
    return OrganizationOwner.objects.filter(organization=org, organization_user__user=user).exists()


def is_admin(user: Any, org: Organization) -> bool:
    if is_owner(user, org):
        return True
    return OrganizationUser.objects.filter(user=user, organization=org, is_admin=True).exists()


def can_manage_members(user: Any, org: Organization) -> bool:
    return is_admin(user, org)


def can_approve_expenses(user: Any, org: Organization) -> bool:
    return is_admin(user, org)


def can_set_policy(user: Any, org: Organization) -> bool:
    return is_admin(user, org)


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="teams.personal_org", weak=False)
def ensure_personal_org(sender, instance, created, **kwargs) -> None:  # noqa: ARG001
    if created and not OrganizationUser.objects.filter(user=instance).exists():
        create_personal_org(instance)
