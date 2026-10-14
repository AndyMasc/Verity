from import_export import resources

from records.models import Record


class RecordResource(resources.ModelResource):
    class Meta:
        model = Record
        exclude = (
            "id",
            "is_active",
            "plaid_transaction_id",
            "plaid_item",
            "expiry_notification_sent",
        )
