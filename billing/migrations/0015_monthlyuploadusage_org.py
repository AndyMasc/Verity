"""Count usage against the org. An org plan buys one shared allowance, so counting
per user would let one member drain it and hand a twenty-person org twenty times
what it paid for."""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill(apps, schema_editor):
    Usage = apps.get_model("billing", "MonthlyUploadUsage")
    PersonalOrg = apps.get_model("teams", "PersonalOrg")
    org_by_user = dict(PersonalOrg.objects.values_list("user_id", "organization_id"))

    for pk, user_id in Usage.objects.values_list("pk", "user_id").iterator():
        org_id = org_by_user.get(user_id)
        if org_id is not None:
            Usage.objects.filter(pk=pk).update(organization_id=org_id)

    # Counters are disposable; drop rows whose user had no personal org.
    Usage.objects.filter(organization__isnull=True).delete()


def unbackfill(apps, schema_editor):
    """Not reversible: the user behind each counter is no longer recorded."""


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0014_alter_orgbilling_customer"),
        ("teams", "0007_personalorg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="monthlyuploadusage", name="unique_monthly_usage"),
        migrations.AddField(
            model_name="monthlyuploadusage",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="monthly_usage",
                to="organizations.organization",
            ),
        ),
        migrations.RunPython(backfill, unbackfill),
        migrations.RemoveField(model_name="monthlyuploadusage", name="user"),
        migrations.AlterField(
            model_name="monthlyuploadusage",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="monthly_usage",
                to="organizations.organization",
            ),
        ),
        migrations.AddConstraint(
            model_name="monthlyuploadusage",
            constraint=models.UniqueConstraint(
                fields=("organization", "period", "action"), name="unique_monthly_usage"
            ),
        ),
    ]
