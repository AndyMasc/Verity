"""Backfill personal organisations for users created before 0005 removed them.

Only users with no organisation at all get one, so nobody who belongs to a team
is given a spurious personal org. Written against historical models only — the
live helper cannot be reused here.
"""

from django.db import migrations
from django.utils.text import slugify

ROLE_ADMIN = True


def forwards(apps, schema_editor):
    User = apps.get_model(*apps.get_model("billing", "CustomUser")._meta.label.split("."))
    Organization = apps.get_model("organizations", "Organization")
    OrganizationUser = apps.get_model("organizations", "OrganizationUser")
    OrganizationOwner = apps.get_model("organizations", "OrganizationOwner")

    for user in User.objects.all().iterator():
        if OrganizationUser.objects.filter(user_id=user.pk).exists():
            continue
        label = f"{user.first_name} {user.last_name}".strip() or user.email
        name = f"{label or 'user'} organisation".strip()
        slug = slugify(name)[:200] or f"user-{user.pk}"
        candidate, n = slug, 1
        while Organization.objects.filter(slug=candidate).exists():
            n += 1
            candidate = f"{slug}-{n}"

        org = Organization.objects.create(name=name, slug=candidate, is_active=True)
        membership = OrganizationUser.objects.create(
            user_id=user.pk, organization_id=org.pk, is_admin=ROLE_ADMIN
        )
        OrganizationOwner.objects.create(organization_id=org.pk, organization_user_id=membership.pk)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("teams", "0005_drop_empty_personal_orgs"),
        ("organizations", "0006_alter_organization_slug"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
