"""Unlink stale "subscriber" customers that the user no longer owns.

A checkout that ran before "Customer.get_or_create" started recording the
"subscriber" could create a second Stripe Customer for a user who already
had one (the legacy row predates the link). When that happens, one of those
Customer rows becomes an orphan: it still has "subscriber" set but the
user's "customer" FK points at a different record.

This data migration reconciles the two sources of truth: for every user,
only the customer referenced by "customuser.customer" may keep
"subscriber" set; any other customer linked to the same user is unlinked
so the write path (which now self-heals too) never double-books the user.
It is intentionally a no-op when the data is already consistent.
"""

from django.db import migrations


def unlink_stale_subscriber_customers(apps, schema_editor):
    CustomUser = apps.get_model("billing", "CustomUser")
    Customer = apps.get_model("djstripe", "Customer")

    for user in CustomUser.objects.exclude(customer__isnull=True).only("id", "customer_id"):
        Customer.objects.filter(subscriber_id=user.id).exclude(id=user.customer_id).update(
            subscriber=None
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0005_alter_customuser_subscription"),
    ]

    operations = [
        migrations.RunPython(unlink_stale_subscriber_customers, migrations.RunPython.noop),
    ]
