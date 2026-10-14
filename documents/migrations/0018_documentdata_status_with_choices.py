# Generated migration for adding status field with comprehensive choices

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0017_remove_documentdata_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentdata",
            name="status",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("pending_upload", "Pending Upload"),
                    ("uploading", "Uploading"),
                    ("uploaded", "Uploaded"),
                    ("processing", "Processing"),
                    ("completed", "Completed"),
                    ("error", "Error"),
                    ("deleting", "Deleting"),
                ],
                default="pending_upload",
            ),
        ),
        migrations.AddIndex(
            model_name="documentdata",
            index=models.Index(fields=["user", "status"], name="documents_doc_user_status_idx"),
        ),
        migrations.AddIndex(
            model_name="documentdata",
            index=models.Index(
                fields=["status", "date_added"], name="documents_doc_status_date_idx"
            ),
        ),
    ]
