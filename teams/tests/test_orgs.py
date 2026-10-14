from django.contrib.auth import get_user_model
from django.test import TestCase
from organizations.models import Organization

from teams.models import Membership
from teams.orgs import NoMembershipError, can, personal_org, role_in

User = get_user_model()


def make_user(email):
    return User.objects.create_user(username=email, email=email, password="x")


class PersonalOrgTest(TestCase):

    def test_new_user_gets_personal_org_with_owner_role(self) -> None:
        user = make_user("solo@test.com")
        membership = Membership.objects.get(user=user)
        self.assertEqual(membership.role, Membership.Role.OWNER)
        self.assertEqual(personal_org(user), membership.organization)

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
        self.assertNotEqual(one.memberships.first().organization.slug,
                            two.memberships.first().organization.slug)

    def test_membership_not_recreated_on_update(self) -> None:
        user = make_user("save@test.com")
        before = Membership.objects.filter(user=user).count()
        user.first_name = "Renamed"
        user.save()
        self.assertEqual(Membership.objects.filter(user=user).count(), before)


class RoleTest(TestCase):

    def setUp(self):
        self.org = Organization.objects.create(name="Acme",
                                               slug="acme",
                                               is_active=True)

    def add(self, email, role):
        user = make_user(email)
        return Membership.objects.create(user=user,
                                         organization=self.org,
                                         role=role)

    def test_role_in_returns_role_or_none(self):
        member = self.add("a@test.com", Membership.Role.FINANCE)
        self.assertEqual(role_in(member.user, self.org),
                         Membership.Role.FINANCE)
        self.assertIsNone(role_in(make_user("b@test.com"), self.org))

    def test_permission_sets(self):
        owner = self.add("o@test.com", Membership.Role.OWNER)
        finance = self.add("f@test.com", Membership.Role.FINANCE)
        employee = self.add("e@test.com", Membership.Role.EMPLOYEE)

        self.assertTrue(
            can(owner.user, self.org, Membership.CAN_MANAGE_MEMBERS))
        self.assertFalse(
            can(employee.user, self.org, Membership.CAN_MANAGE_MEMBERS))
        self.assertTrue(
            can(finance.user, self.org, Membership.CAN_APPROVE_EXPENSES))
        self.assertFalse(
            can(employee.user, self.org, Membership.CAN_APPROVE_EXPENSES))
        self.assertTrue(can(owner.user, self.org, Membership.CAN_SET_POLICY))
        self.assertFalse(can(finance.user, self.org,
                             Membership.CAN_SET_POLICY))

    def test_one_membership_per_user_per_org(self):
        from django.db import IntegrityError, transaction

        user = make_user("dupe@test.com")
        Membership.objects.create(user=user,
                                  organization=self.org,
                                  role=Membership.Role.EMPLOYEE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Membership.objects.create(user=user,
                                      organization=self.org,
                                      role=Membership.Role.ADMIN)


class NoMembershipTest(TestCase):

    def test_personal_org_raises_when_user_has_none(self):
        user = User.objects.create_user(username="ghost",
                                        email="ghost@test.com",
                                        password="x")
        Membership.objects.filter(user=user).delete()
        with self.assertRaises(NoMembershipError):
            personal_org(user)
