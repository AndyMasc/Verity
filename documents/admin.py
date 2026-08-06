from django.contrib import admin, messages

from .models import DocumentData


@admin.action(description="Hard-delete selected documents (permanent)")
def hard_delete_documents(modeladmin, request, queryset):  # noqa: ARG001
    if not request.user.is_superuser:
        messages.error(request, "Only superusers can hard-delete documents.")
        return
    count = queryset.count()
    for doc in queryset:
        doc.hard_delete()
    messages.success(request, f"Permanently deleted {count} document(s).")


class DocumentDataAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "status", "did_ocr")
    list_filter = ("status", "did_ocr")
    search_fields = ("title",)
    actions = [hard_delete_documents]

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not request.user.is_superuser:
            del actions["hard_delete_documents"]
            del actions["delete_selected"]
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

    def has_delete_permission(self, request, obj=None):  # noqa: ARG002
        return request.user.is_superuser

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.for_user(request.user)


admin.site.register(DocumentData, DocumentDataAdmin)
