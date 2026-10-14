"""Give every pre-existing user a personal organisation and an owner membership.

Runs before any org FK lands on domain models, so there is nothing to
backfill there yet; existing records are scoped once those columns exist.
Reversible: drops the memberships and the personal organisations only.
"""

from django.db import migrations
from django.utils.text import slugify

ROLE_OWNER = "owner"


def personal_org_name(user) -> str:
    # Fields, not get_full_name: historical models in migration state do not
    # carry AbstractUser's methods.
    label = f"{user.first_name} {user.last_name}".strip() or user.email
    return f"{label or 'user-%s' % user.pk}'s organisation"


def unique_slug(model, base: str, pk: int) -> str:
    """Personal orgs can collide on name (two 'Wendy'), so suffix on conflict."""
    slug = slugify(base)[:200] or f"user-{pk}"
    candidate, n = slug, 1
    while model.objects.filter(slug=candidate).exists():
        n += 1
        candidate = f"{slug}-{n}"
    return candidate


def forwards(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Membership = apps.get_model("teams", "Membership")
    User = apps.get_model(
        *apps.get_model("billing", "CustomUser")._meta.label.split("."))

    for user in User.objects.all().iterator():
        org = Organization.objects.create(
            name=personal_org_name(user),
            slug=unique_slug(Organization, personal_org_name(user), user.pk),
            is_active=True,
        )
        Membership.objects.create(user=user, organization=org, role=ROLE_OWNER)


def backwards(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Membership = apps.get_model("teams", "Membership")
    User = apps.get_model(
        *apps.get_model("billing", "CustomUser")._meta.label.split("."))

    for user in User.objects.all().iterator():
        org_ids = Membership.objects.filter(user=user).values_list(
            "organization_id", flat=True)
        Organization.objects.filter(pk__in=list(org_ids)).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0006_alter_organization_slug"),
        ("teams", "0001_initial"),
        ("billing", "0011_rename_monthlyusage_monthlyuploadusage"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
