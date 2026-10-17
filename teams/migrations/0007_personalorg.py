"""Name each user's solo org, replacing the "owned org with one member" heuristic
that stops recognising the org once a second person joins. The backfill reproduces
that heuristic so existing users keep their billing subject."""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    PersonalOrg = apps.get_model("teams", "PersonalOrg")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    for user_id in User.objects.values_list("id", flat=True).iterator():
        org_id = (
            Organization.objects.filter(owner__organization_user__user_id=user_id)
            .annotate(members=models.Count("organization_users", distinct=True))
            .filter(members=1)
            .values_list("pk", flat=True)
            .first()
        )
        if org_id is not None:
            PersonalOrg.objects.get_or_create(user_id=user_id, organization_id=org_id)


def unbackfill(apps, schema_editor):
    apps.get_model("teams", "PersonalOrg").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("teams", "0006_backfill_personal_orgs"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonalOrg",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="personal_for",
                        to="organizations.organization",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="personal_org",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.RunPython(backfill, unbackfill),
    ]
