from django.contrib import admin

from .models import CustomUser, ScanUsage


@admin.register(ScanUsage)
class ScanUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "period", "count")
    list_filter = ("period",)
    search_fields = ("user__email", "user__username")
    readonly_fields = ("count",)


@admin.register(CustomUser)
class CustomUserAdmin(admin.ModelAdmin):
    list_display = ("email", "username", "is_active", "is_staff", "is_superuser")
    list_filter = ("is_active", "is_staff", "is_superuser")
    search_fields = ("email", "username")
    readonly_fields = ("date_joined", "last_login")
