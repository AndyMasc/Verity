from django.contrib.auth import get_user_model
from django.test import TestCase
from organizations.models import Organization, OrganizationOwner, OrganizationUser

from teams.orgs import (NoMembershipError, can_approve_expenses,
                        can_manage_members, is_admin, is_owner, personal_org)

User = get_user_model()


def make_user(email, username=None):
    return User.objects.create_user(username=username or email,
                                    email=email,
                                    password="x")


def add(user, org, *, admin=False, owner=False):
    org_user = OrganizationUser.objects.create(user=user,
                                               organization=org,
                                               is_admin=admin or owner)
    if owner:
        OrganizationOwner.objects.create(organization=org,
                                         organization_user=org_user)
    return org_user


class PersonalOrgTest(TestCase):

    def test_new_user_gets_personal_org_as_owner(self) -> None:
        user = make_user("solo@test.com")
        org = personal_org(user)
        self.assertTrue(is_owner(user, org))
        self.assertTrue(is_admin(user, org))

    def test_org_names_do_not_collide(self) -> None:
        one = User.objects.create_user(username="w1",
                                       email="w1@test.com",
                                       password="x",
                                       first_name="Wendy",
                                       last_name="M")
        two = User.objects.create_user(username="w2",
                                       email="w2@test.com",
                                       password="x",
                                       first_name="Wendy",
                                       last_name="M")
        self.assertNotEqual(personal_org(one).slug, personal_org(two).slug)

    def test_personal_org_raises_without_membership(self) -> None:
        user = make_user("ghost@test.com")
        OrganizationUser.objects.filter(user=user).delete()
        with self.assertRaises(NoMembershipError):
            personal_org(user)


class PermissionTest(TestCase):

    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Acme",
                                               slug="acme",
                                               is_active=True)
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
        self.assertFalse(is_owner(self.member, self.org))
        self.assertFalse(is_admin(self.member, self.org))

    def test_outsider_has_no_permissions(self) -> None:
        outsider = make_user("out@test.com", username="x")
        self.assertFalse(can_manage_members(outsider, self.org))
        self.assertFalse(can_approve_expenses(outsider, self.org))


class InvitationWiringTest(TestCase):

    def test_add_user_creates_org_user_row(self) -> None:
        owner = make_user("o@test.com", username="o")
        org = personal_org(owner)
        member = make_user("m@test.com", username="m")
        org.add_user(member)
        self.assertTrue(
            OrganizationUser.objects.filter(user=member,
                                            organization=org).exists())
        self.assertFalse(is_admin(member, org))

    def test_first_user_added_becomes_owner(self) -> None:
        org = Organization.objects.create(name="Fresh",
                                          slug="fresh",
                                          is_active=True)
        member = make_user("m@test.com", username="m")
        org.add_user(member)
        self.assertTrue(is_owner(member, org))
