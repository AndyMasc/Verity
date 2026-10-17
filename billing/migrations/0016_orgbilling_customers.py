"""Give every org a billing row and move existing customers onto their org. Billing
resolves through OrgBilling instead of Customer.subscriber, so without this every
current paying user would drop to Free the moment the lookup moved."""

from django.conf import settings
from django.db import migrations


def backfill(apps, schema_editor):
    Customer = apps.get_model("djstripe", "Customer")
    Organization = apps.get_model("organizations", "Organization")
    OrgBilling = apps.get_model("billing", "OrgBilling")
    PersonalOrg = apps.get_model("teams", "PersonalOrg")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))

    for org_id in Organization.objects.values_list("pk", flat=True).iterator():
        OrgBilling.objects.get_or_create(organization_id=org_id)

    # User.customer and OrgBilling.customer key on different columns: the user FK
    # targets djstripe_id, the org FK targets the Stripe id. Map between them
    # rather than copying one into the other.
    by_djstripe_id = dict(Customer.objects.values_list("djstripe_id", "id"))
    by_subscriber = dict(Customer.objects.values_list("subscriber_id", "id"))

    for user_id, org_id in PersonalOrg.objects.values_list("user_id", "organization_id"):
        user_customer_pk = (
            User.objects.filter(pk=user_id).values_list("customer_id", flat=True).first()
        )
        customer_id = by_djstripe_id.get(user_customer_pk) or by_subscriber.get(user_id)
        if customer_id:
            OrgBilling.objects.filter(organization_id=org_id).update(customer_id=customer_id)


def unbackfill(apps, schema_editor):
    apps.get_model("billing", "OrgBilling").objects.update(customer=None)


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0015_monthlyuploadusage_org"),
        ("teams", "0007_personalorg"),
        ("djstripe", "0003_2_11"),
        ("organizations", "0006_alter_organization_slug"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(backfill, unbackfill)]
