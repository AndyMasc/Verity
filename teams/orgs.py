from typing import Any

from organizations.models import Organization, OrganizationOwner, OrganizationUser


class NoMembershipError(Exception):
    """A user was asked for their organisation but does not belong to one."""


def personal_org(user: Any) -> Organization:
    """The organisation a user belongs to.

    Every account gets a personal organisation on creation (teams.0002), so
    this is unambiguous today. Once people join more than one, callers that
    mean "an org I picked" must pass it explicitly.
    """
    membership = OrganizationUser.objects.filter(
        user=user).order_by("created").first()
    if membership is None:
        raise NoMembershipError(f"user {user.pk} belongs to no organisation")
    return membership.organization


def is_owner(user: Any, organization: Organization) -> bool:
    return OrganizationOwner.objects.filter(
        organization=organization, organization_user__user=user).exists()


def is_admin(user: Any, organization: Organization) -> bool:
    """Owners are admins; so is anyone with is_admin set."""
    if is_owner(user, organization):
        return True
    return OrganizationUser.objects.filter(user=user,
                                           organization=organization,
                                           is_admin=True).exists()


def can_manage_members(user: Any, organization: Organization) -> bool:
    return is_admin(user, organization)


def can_approve_expenses(user: Any, organization: Organization) -> bool:
    return is_admin(user, organization)


def can_set_policy(user: Any, organization: Organization) -> bool:
    return is_admin(user, organization)
