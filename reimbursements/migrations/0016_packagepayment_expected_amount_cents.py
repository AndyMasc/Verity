from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "reimbursements",
            "0015_alter_packagepayment_stripe_payment_intent_id_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="packagepayment",
            name="expected_amount_cents",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
    ]
