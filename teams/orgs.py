"""Personal organisations, billing subjects, and per-org permissions.
A personal org is created at signup and is the billing subject for that user's
own account, so billing never branches on user-versus-org -- the answer is always
an org. Team orgs are separate subjects with their own Stripe customer.
"""

from django.conf import settings
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils.text import slugify
from organizations.models import Organization, OrganizationOwner, OrganizationUser

from billing.models import OrgBilling

from .models import PersonalOrg


def create_personal_org(user) -> Organization:
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
    PersonalOrg.objects.create(organization=org, user=user)
    return org


def billing_org(user) -> Organization | None:
    """The org holding this user's own subscription."""
    if not user or not user.is_authenticated:
        return None
    try:
        return user.personal_org.organization
    except PersonalOrg.DoesNotExist:
        return None


def entitled_orgs(user) -> list[Organization]:
    """Every org whose plan the user runs on: their own, plus any team they are in.
    Membership alone is enough -- an employer paying for a team is why the member
    runs on that plan at all.
    """
    if not user or not user.is_authenticated:
        return []
    return list(
        Organization.objects.filter(
            Q(personal_for__user=user) | Q(organization_users__user=user)
        ).distinct()
    )


def is_owner(user, org) -> bool:
    return OrganizationOwner.objects.filter(organization=org, organization_user__user=user).exists()


def is_admin(user, org) -> bool:
    return (
        is_owner(user, org)
        or OrganizationUser.objects.filter(user=user, organization=org, is_admin=True).exists()
    )


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="teams.personal_org", weak=False)
def ensure_personal_org(sender, instance, created, **kwargs) -> None:  # noqa: ARG001
    if created and not OrganizationUser.objects.filter(user=instance).exists():
        create_personal_org(instance)


@receiver(post_save, sender=Organization, dispatch_uid="teams.org_billing", weak=False)
def ensure_org_billing(sender, instance, created, **kwargs) -> None:  # noqa: ARG001
    """Team orgs are created by django-organizations' own views, so the billing row
    has to come from the model rather than a call site we control. The Stripe
    customer attaches later, at checkout."""
    if created:
        OrgBilling.objects.get_or_create(organization=instance)
