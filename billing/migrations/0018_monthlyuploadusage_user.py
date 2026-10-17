"""Count usage per member, for a calendar month."""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0017_monthlyuploadusage_member"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(model_name="monthlyuploadusage", name="unique_monthly_usage"),
        migrations.DeleteModel(name="MonthlyUploadUsage"),
        migrations.CreateModel(
            name="MonthlyUploadUsage",
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
                    "period",
                    models.CharField(help_text="Calendar month, e.g. 2026-08", max_length=7),
                ),
                (
                    "action",
                    models.CharField(
                        help_text="The type of action being tracked. E.g. 'scan' or 'upload'.",
                        max_length=16,
                    ),
                ),
                ("count", models.PositiveIntegerField(default=0)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="monthly_usage",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"abstract": None},
        ),
        migrations.AddConstraint(
            model_name="monthlyuploadusage",
            constraint=models.UniqueConstraint(
                fields=("user", "period", "action"), name="unique_monthly_usage"
            ),
        ),
    ]
