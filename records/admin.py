"""Django admin configuration for the records module.

Registers Record and Folder with custom admin classes. Restricts
destructive actions (hard-delete, soft-delete) to superusers while
keeping the rest of the admin read-safe for regular staff.
"""

from django.contrib import admin, messages

from .models import Folder, Record


@admin.action(description="Hard-delete selected records (permanent)")
def hard_delete_records(modeladmin, request, queryset):  # noqa: ARG001
    """Permanently delete selected records. Superuser-only action."""
    if not request.user.is_superuser:
        messages.error(request, "Only superusers can hard-delete records.")
        return
    count = queryset.count()
    for record in queryset:
        record.hard_delete()
    messages.success(request, f"Permanently deleted {count} record(s).")


def safe_delete_selected(modeladmin, request, queryset):  # noqa: ARG001
    """Soft-delete selected records via the admin action menu."""
    for obj in queryset:
        obj.delete()
    messages.success(request, f"Soft-deleted {queryset.count()} record(s).")


class RecordAdmin(admin.ModelAdmin):
    """Admin class for Record with superuser-gated destructive actions."""

    list_display = ("title", "user", "is_active", "last_edited")
    list_filter = ("is_active", "record_type")
    search_fields = ("title", "merchant")
    actions = [hard_delete_records]

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            if "hard_delete_records" in actions:
                del actions["hard_delete_records"]
            if "delete_selected" in actions:
                del actions["delete_selected"]
        else:
            if "delete_selected" in actions:
                # Fix: Extract tuple properties safely and overwrite with a new tuple
                _func, name, description = actions["delete_selected"]
                actions["delete_selected"] = (safe_delete_selected, name, description)
        return actions

    def delete_model(self, request, obj):
        if request.user.is_superuser:
            obj.hard_delete()
        else:
            obj.delete()

    def delete_queryset(self, request, queryset):
        if request.user.is_superuser:
            for obj in queryset:
                obj.hard_delete()
        else:
            for obj in queryset:
                obj.delete()

    def get_deleted_objects(self, queryset, request):
        # Fix: Updated signature from old (objs, request) to modern (queryset, request)
        deleted, protected, perms_needed, view_only = super().get_deleted_objects(queryset, request)
        return deleted, protected, perms_needed, view_only

    def has_delete_permission(self, request, obj=None):  # noqa: ARG002
        return request.user.is_superuser


admin.site.register(Record, RecordAdmin)
admin.site.register(Folder)
