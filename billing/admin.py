from django.contrib import admin

from .models import ScanUsage


@admin.register(ScanUsage)
class ScanUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "period", "count")
    list_filter = ("period",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("count",)
