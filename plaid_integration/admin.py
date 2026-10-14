from django.contrib import admin

from .models import PlaidItem


@admin.register(PlaidItem)
class PlaidItemAdmin(admin.ModelAdmin):
    list_display = ("user", "item_id", "next_cursor", "record_count", "created_at")
    search_fields = ("user__email", "item_id")
    raw_id_fields = ("user",)

    @admin.display(description="Records")
    def record_count(self, obj):
        return obj.records.count()
