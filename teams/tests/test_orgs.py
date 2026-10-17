from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse
from organizations.models import Organization, OrganizationOwner, OrganizationUser

from billing import entitlements, features, metadata
from billing.tests.helpers import give_feature
from teams.forms import OrganizationCreateForm, OrganizationUserAddForm

from teams.orgs import billing_org, entitled_orgs, is_admin, is_owner

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
        org = billing_org(user)
        self.assertIsNotNone(org)
        self.assertEqual(org.organization_users.count(), 1)
        self.assertTrue(is_owner(user, org))

    def test_every_org_has_a_billing_row(self) -> None:
        org = billing_org(make_user("billing@test.com"))
        self.assertIsNotNone(org.billing)

    def test_team_org_also_gets_a_billing_row(self) -> None:
        org = Organization.objects.create(name="Team", slug="team", is_active=True)
        self.assertIsNotNone(org.billing)

    def test_adding_a_member_does_not_break_the_billing_org(self) -> None:
        """The personal org stays the billing subject once it has a team in it."""
        user = make_user("grown@test.com")
        org = billing_org(user)
        add(make_user("second@test.com", username="second"), org)
        self.assertEqual(billing_org(user).pk, org.pk)

    def test_entitled_orgs_include_the_own_org_and_every_team(self) -> None:
        user = make_user("entitled@test.com")
        team = Organization.objects.create(name="Team", slug="entitled-team", is_active=True)
        add(user, team)
        self.assertEqual(
            {org.pk for org in entitled_orgs(user)},
            {billing_org(user).pk, team.pk},
        )


class PermissionTest(TestCase):
    def setUp(self) -> None:
        self.org = Organization.objects.create(name="Acme", slug="acme", is_active=True)
        self.owner = make_user("owner@test.com", username="o")
        self.admin = make_user("admin@test.com", username="a")
        self.member = make_user("member@test.com", username="m")
        add(self.owner, self.org, owner=True)
        add(self.admin, self.org, admin=True)
        add(self.member, self.org)

    def test_owner_and_admin_are_admins(self) -> None:
        self.assertTrue(is_admin(self.owner, self.org))
        self.assertTrue(is_admin(self.admin, self.org))

    def test_member_and_outsider_are_not_admins(self) -> None:
        outsider = make_user("out@test.com", username="x")
        self.assertFalse(is_admin(self.member, self.org))
        self.assertFalse(is_owner(self.member, self.org))
        self.assertFalse(is_admin(outsider, self.org))

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


class OrgCreationGateTests(TestCase):
    """Creating a team org is gated on the create-orgs feature."""

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def setUp(self) -> None:
        self.user = make_user("gated@test.com")
        self.url = reverse("organization_add")

    def test_anonymous_is_sent_to_login(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_plan_without_the_feature_is_sent_to_pricing(self) -> None:
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("billing:pricing_page"))

    def test_plan_with_the_feature_reaches_the_form(self) -> None:
        give_feature(billing_org(self.user), metadata.ORGANIZATIONS.stripe_id, "create-orgs")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_form_refuses_to_build_an_org_without_the_feature(self) -> None:
        """The form is the gate on the write, whichever view reaches it."""
        request = self._request(self.user)
        form = OrganizationCreateForm(
            request=request, data={"name": "Acme", "slug": "acme", "email": "owner@acme.com"}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Upgrade", form.errors["__all__"][0])
        self.assertFalse(Organization.objects.filter(name="Acme").exists())

    def test_form_accepts_an_org_once_the_feature_is_held(self) -> None:
        give_feature(billing_org(self.user), metadata.ORGANIZATIONS.stripe_id, "create-orgs")
        request = self._request(self.user)
        form = OrganizationCreateForm(
            request=request, data={"name": "Acme", "slug": "acme", "email": "owner@acme.com"}
        )
        self.assertTrue(form.is_valid(), form.errors)


class AddMemberGateTests(TestCase):
    """Adding a member is gated on add-org-members, a key separate from
    create-orgs, and on being an admin of the org."""

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def setUp(self) -> None:
        self.owner = make_user("adder@test.com", username="adder")
        self.org = Organization.objects.create(name="Acme", slug="acme-add", is_active=True)
        add(self.owner, self.org, owner=True)
        self.url = reverse("organization_user_add", kwargs={"organization_pk": self.org.pk})

    def _get(self):
        self.client.force_login(self.owner)
        return self.client.get(self.url)

    def test_without_the_feature_the_owner_is_sent_to_pricing(self) -> None:
        self.assertRedirects(self._get(), reverse("billing:pricing_page"))

    def test_create_orgs_alone_does_not_unlock_adding(self) -> None:
        give_feature(self.org, metadata.ORGANIZATIONS.stripe_id, "create-orgs")
        self.assertRedirects(self._get(), reverse("billing:pricing_page"))

    def test_with_the_feature_the_owner_reaches_the_form(self) -> None:
        give_feature(self.org, metadata.ORGANIZATIONS.stripe_id, "add-org-members")
        self.assertEqual(self._get().status_code, 200)

    def test_a_stranger_is_turned_away(self) -> None:
        give_feature(self.org, metadata.ORGANIZATIONS.stripe_id, "add-org-members")
        stranger = make_user("stranger@test.com", username="stranger")
        self.client.force_login(stranger)
        self.assertNotEqual(self.client.get(self.url).status_code, 200)

    def test_form_refuses_without_the_feature(self) -> None:
        form = OrganizationUserAddForm(
            request=self._request(self.owner),
            organization=self.org,
            data={"email": "new@acme.com", "is_admin": False},
        )
        self.assertFalse(form.is_valid())
        self.assertIn("Upgrade", form.errors["__all__"][0])
        self.assertEqual(self.org.organization_users.count(), 1)

    def test_form_accepts_with_the_feature(self) -> None:
        give_feature(self.org, metadata.ORGANIZATIONS.stripe_id, "add-org-members")
        form = OrganizationUserAddForm(
            request=self._request(self.owner),
            organization=self.org,
            data={"email": "new@acme.com", "is_admin": False},
        )
        self.assertTrue(form.is_valid(), form.errors)


class SponsorshipTests(TestCase):
    """A buyer pays once; every member of the org they created runs on that plan."""

    def setUp(self) -> None:
        self.sponsor = make_user("sponsor@test.com", username="sponsor")
        self.member = make_user("member@test.com", username="member")
        give_feature(billing_org(self.sponsor), metadata.ORGANIZATIONS.stripe_id, "create-orgs")

    def _create_org(self) -> Organization:
        form = OrganizationCreateForm(
            request=self._request(self.sponsor),
            data={"name": "Acme", "slug": "acme-sponsored", "email": "sponsor@test.com"},
        )
        assert form.is_valid(), form.errors
        return form.save()

    def _request(self, user):
        request = RequestFactory().get("/")
        request.user = user
        return request

    def test_creating_an_org_records_who_sponsors_it(self) -> None:
        org = self._create_org()
        sponsored = org.sponsored_for
        self.assertEqual(sponsored.sponsor, self.sponsor)
        self.assertEqual(sponsored.product_id, metadata.ORGANIZATIONS.stripe_id)

    def test_members_inherit_the_sponsors_plan(self) -> None:
        org = self._create_org()
        add(self.member, org)
        self.assertEqual(
            entitlements.get_monthly_upload_limit(self.member, "upload"),
            features.PRO_MONTHLY_UPLOAD_LIMIT,
        )
        self.assertTrue(entitlements.has_feature(self.member, "create-orgs"))

    def test_a_stranger_gets_nothing_from_the_org(self) -> None:
        self._create_org()
        self.assertEqual(
            entitlements.get_monthly_upload_limit(self.member, "upload"),
            features.FREE_MONTHLY_UPLOAD_LIMIT,
        )

    def test_the_sponsorship_ends_when_the_sponsor_lets_go(self) -> None:
        org = self._create_org()
        add(self.member, org)
        self.assertEqual(
            entitlements.get_monthly_upload_limit(self.member, "upload"),
            features.PRO_MONTHLY_UPLOAD_LIMIT,
        )

        org.sponsored_for.delete()
        # Re-fetch: plan_for_user caches on the instance, which is per-request in
        # production but persists across assertions here.
        member = User.objects.get(pk=self.member.pk)
        self.assertEqual(
            entitlements.get_monthly_upload_limit(member, "upload"),
            features.FREE_MONTHLY_UPLOAD_LIMIT,
        )
