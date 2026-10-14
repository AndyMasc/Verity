from django.contrib.auth import get_user_model
from django.test import TestCase
from organizations.models import Organization, OrganizationOwner, OrganizationUser

from teams.orgs import (
    can_approve_expenses,
    can_manage_members,
    can_set_policy,
    is_admin,
    is_owner,
    personal_org,
)

User = get_user_model()


def make_user(email, username=None):
    return User.objects.create_user(username=username or email, email=email, password="x")


def add(user, org, *, admin=False, owner=False):
    org_user = OrganizationUser.objects.create(user=user, organization=org, is_admin=admin or owner)
    if owner:
        OrganizationOwner.objects.create(organization=org, organization_user=org_user)
    return org_user


class PersonalOrgTest(TestCase):
    """Every account gets one org they own alone: the solo billing subject."""

    def test_new_user_gets_a_solo_org(self) -> None:
        user = make_user("solo@test.com")
        org = personal_org(user)
        self.assertIsNotNone(org)
        self.assertEqual(org.organization_users.count(), 1)
        self.assertTrue(is_owner(user, org))


class PermissionTest(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Acme", slug="acme", is_active=True)
        self.owner = make_user("owner@test.com", username="o")
        self.admin = make_user("admin@test.com", username="a")
        self.member = make_user("member@test.com", username="m")
        add(self.owner, self.org, owner=True)
        add(self.admin, self.org, admin=True)
        add(self.member, self.org)

    def test_owner_and_admin_can_manage(self) -> None:
        self.assertTrue(can_manage_members(self.owner, self.org))
        self.assertTrue(can_manage_members(self.admin, self.org))

    def test_member_cannot_manage_or_approve(self) -> None:
        self.assertFalse(can_manage_members(self.member, self.org))
        self.assertFalse(can_approve_expenses(self.member, self.org))
        self.assertFalse(can_set_policy(self.member, self.org))
        self.assertFalse(is_owner(self.member, self.org))
        self.assertFalse(is_admin(self.member, self.org))

    def test_outsider_has_no_permissions(self) -> None:
        outsider = make_user("out@test.com", username="x")
        self.assertFalse(can_manage_members(outsider, self.org))
        self.assertFalse(can_approve_expenses(outsider, self.org))

    def test_owner_cannot_be_deleted(self) -> None:
        from organizations.exceptions import OwnershipRequired

        owner_membership = OrganizationUser.objects.get(user=self.owner, organization=self.org)
        with self.assertRaises(OwnershipRequired):
            owner_membership.delete()


class OrgWithoutPersonalNamingTest(TestCase):
    def test_org_names_do_not_collide(self) -> None:
        one = Organization.objects.create(name="Wendy M", slug="wendy-m", is_active=True)
        two = Organization.objects.create(name="Wendy M", slug="wendy-m-2", is_active=True)
        self.assertNotEqual(one.slug, two.slug)

    def test_inactive_org_is_soft(self) -> None:
        org = Organization.objects.create(name="Old", slug="old", is_active=False)
        self.assertFalse(Organization.active.filter(pk=org.pk).exists())
        self.assertTrue(Organization.objects.filter(pk=org.pk).exists())
