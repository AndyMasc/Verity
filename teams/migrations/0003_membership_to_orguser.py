"""Replace the four-role Membership with django-organizations' OrganizationUser.

is_admin covers owner/admin and member. Prior owners become OrganizationOwner
rows so ownership stays explicit and their delete() guard applies.
"""

from django.db import migrations

ADMIN_ROLES = ("owner", "admin")


def forwards(apps, schema_editor):
    Membership = apps.get_model("teams", "Membership")
    OrganizationOwner = apps.get_model("organizations", "OrganizationOwner")
    OrganizationUser = apps.get_model("organizations", "OrganizationUser")

    for m in Membership.objects.all().iterator():
        is_admin = m.role in ADMIN_ROLES
        org_user, _ = OrganizationUser.objects.get_or_create(
            user_id=m.user_id,
            organization_id=m.organization_id,
            defaults={"is_admin": is_admin},
        )
        if m.role == "owner":
            OrganizationOwner.objects.get_or_create(
                organization_id=m.organization_id, organization_user=org_user)


def backwards(apps, schema_editor):
    OrganizationUser = apps.get_model("organizations", "OrganizationUser")
    OrganizationOwner = apps.get_model("organizations", "OrganizationOwner")
    Membership = apps.get_model("teams", "Membership")

    for org_user in OrganizationUser.objects.all().iterator():
        owned = OrganizationOwner.objects.filter(
            organization_id=org_user.organization_id,
            organization_user=org_user).exists()
        Membership.objects.create(
            user_id=org_user.user_id,
            organization_id=org_user.organization_id,
            role="owner" if owned else
            ("admin" if org_user.is_admin else "employee"),
        )


class Migration(migrations.Migration):

    dependencies = [
        ("teams", "0002_personal_orgs"),
        ("organizations", "0006_alter_organization_slug"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
