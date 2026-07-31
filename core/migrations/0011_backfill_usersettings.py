from django.conf import settings
from django.db import migrations


def backfill_user_settings(apps, schema_editor):
    User = apps.get_model(settings.AUTH_USER_MODEL)
    UserSettings = apps.get_model("core", "UserSettings")

    existing_user_ids = UserSettings.objects.values_list("user_id", flat=True)
    users_without_settings = User.objects.exclude(id__in=existing_user_ids)

    UserSettings.objects.bulk_create(
        [UserSettings(user=user) for user in users_without_settings.iterator()],
        ignore_conflicts=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0010_alter_usersettings_default_currency"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill_user_settings, migrations.RunPython.noop),
    ]
