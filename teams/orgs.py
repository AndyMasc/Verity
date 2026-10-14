from typing import Any

from organizations.models import Organization

from .models import Membership


class NoMembershipError(Exception):
    """A user was asked for their organisation but has none."""


def personal_org(user: Any) -> Organization:
    """The organisation a user authors into.

    Every account gets exactly one personal organisation (teams.0002), so
    this is unambiguous today. Once users belong to several organisations,
    callers that mean "an org I chose" must pass it explicitly instead.
    """
    membership = Membership.objects.filter(
        user=user).order_by("created_at").first()
    if membership is None:
        raise NoMembershipError(
            f"user {user.pk} has no organisation membership")
    return membership.organization


def role_in(user: Any, organization: Organization) -> Membership.Role | None:
    membership = Membership.objects.filter(user=user,
                                           organization=organization).first()
    return membership.role if membership else None


def can(user: Any, organization: Organization, allowed: frozenset) -> bool:
    return role_in(user, organization) in allowed
