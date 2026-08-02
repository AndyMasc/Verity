"""Filtersets for the document list view.

Provides dynamic filter choices for file type, processing status, and
active/trash state. File-type choices are cached per user to avoid repeated queries.
"""

import django_filters
from django import forms
from django.core.cache import cache

from .models import DocumentData

FILTER_CHOICES_CACHE_TTL = 3600


class DocumentFilter(django_filters.FilterSet):
    """Filters documents by activity state, file extension, and link status.

    File type choices are populated dynamically from the user's existing
    documents and cached to reduce database load on list views.
    """

    is_active = django_filters.BooleanFilter(
        field_name="is_active",
        lookup_expr="exact",
        widget=forms.Select(choices=[(None, "All"), (True, "Active"), (False, "Trash")]),
    )

    file_type = django_filters.ChoiceFilter(
        field_name="file_extension",
        lookup_expr="iexact",
        choices=(),
        widget=forms.Select(),
        label="File Type",
    )

    status = django_filters.ChoiceFilter(
        choices=(
            ("orphaned", "Orphaned (Unlinked)"),
            ("processed_unsaved", "Processed (Unsaved)"),
            ("linked", "Associated Records"),
        ),
        method="filter_by_status",
        widget=forms.Select(),
        label="Status",
    )

    class Meta:
        model = DocumentData
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        extensions = self._get_cached_extensions()
        self.filters["file_type"].extra["choices"] = [("", "All File Types")] + [
            (ext.lower(), ext.upper())
            for ext in extensions
            if ext and ext.isalnum() and len(ext) <= 10
        ]

    def _get_cached_extensions(self):
        """Return distinct file extensions for the user, using cache to avoid repeated queries."""
        if self.request and self.request.user.is_authenticated:
            cache_key = f"de_v3_{self.request.user.id}"
            extensions = cache.get(cache_key)
            if extensions is None:
                extensions = sorted(
                    {
                        ext.strip().lower()[:10]
                        for ext in DocumentData.objects.filter(user=self.request.user)
                        .values_list("file_extension", flat=True)
                        .distinct()
                        if ext and ext.strip()
                    }
                )
                cache.set(cache_key, extensions, FILTER_CHOICES_CACHE_TTL)
            return extensions
        return []

    def filter_by_status(self, queryset, name, value):  # noqa: ARG002
        """Filter by composite status: orphaned, processed_unsaved, or linked."""
        if value == "orphaned":
            return queryset.filter(associated_record__isnull=True)
        elif value == "processed_unsaved":
            return queryset.filter(
                associated_record__isnull=True,
                status="completed",
            )
        elif value == "linked":
            return queryset.filter(associated_record__isnull=False)
        return queryset
