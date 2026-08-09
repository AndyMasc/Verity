"""Django-filters FilterSets for record list views.

Provides the filter definitions used by RecordListView, including
per-user caching of folder and record-type choices to avoid repeated
database queries on every page load.
"""

import django_filters
from django import forms
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .models import Folder, MergeLog, Record

FILTER_CHOICES_CACHE_TTL = 3600


class RecordFilter(django_filters.FilterSet):
    """FilterSet for the record list view with dynamic folder and type choices.

    Folder and record-type dropdowns are populated per-user and cached for
    one hour. The "expiring_soon" and "this_month" filters are boolean
    toggles backed by custom methods rather than simple ORM lookups.
    """

    expiring_soon = django_filters.BooleanFilter(
        method="filter_expiring_soon",
        label="Expiring within 30 days",
        field_name="is_expiring_soon",
        lookup_expr="exact",
        widget=forms.Select(choices=[(False, "Current"), (True, "Expiring Soon")]),
    )

    is_active = django_filters.BooleanFilter(
        field_name="is_active",
        lookup_expr="exact",
        widget=forms.Select(choices=[(None, "All"), (True, "Active"), (False, "Archived")]),
    )

    record_type = django_filters.ChoiceFilter(
        field_name="record_type",
        widget=forms.Select(),
    )

    folder = django_filters.ChoiceFilter(
        method="filter_by_folder",
        empty_label="All Folders",
        widget=forms.Select(),
    )

    this_month = django_filters.BooleanFilter(
        method="filter_this_month",
        label="Records from this month",
        field_name="this_month_records",
        widget=forms.Select(choices=[(False, "All Time"), (True, "This Month")]),
    )

    merged = django_filters.BooleanFilter(
        method="filter_merged",
        label="Merged",
        widget=forms.Select(choices=[(False, "All Records"), (True, "Merged")]),
    )

    shared = django_filters.BooleanFilter(
        method="filter_shared",
        label="Shared with me",
        widget=forms.Select(choices=[(False, "All Records"), (True, "Shared with me")]),
    )

    class Meta:
        model = Record
        fields = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.request and self.request.user.is_authenticated:
            folder_filter = self.filters.get("folder") or self.base_filters.get("folder")
            if folder_filter:
                cache_key = f"folder_choices_{self.request.user.id}"
                user_folders = cache.get(cache_key)
                if user_folders is None:
                    user_folders = list(
                        Folder.objects.filter(user=self.request.user).values_list("id", "name")
                    )
                    cache.set(cache_key, user_folders, FILTER_CHOICES_CACHE_TTL)
                folder_filter.extra["choices"] = [
                    ("none", "All folders"),
                    *user_folders,
                ]

            cache_key = f"rt_{self.request.user.id}"
            user_record_types = cache.get(cache_key)
            if user_record_types is None:
                user_record_types = set(
                    Record.objects.filter(user=self.request.user, is_active=True)
                    .values_list("record_type", flat=True)
                    .distinct()
                )
                cache.set(cache_key, user_record_types, FILTER_CHOICES_CACHE_TTL)

            all_choices = Record.RecordTypes.choices
            if user_record_types:
                filtered = [
                    (value, label) for value, label in all_choices if value in user_record_types
                ]
            else:
                filtered = list(all_choices)

            type_filter = self.filters.get("record_type") or self.base_filters.get("record_type")
            if type_filter:
                type_filter.extra["choices"] = [("", "All Types"), *filtered]

    def filter_by_folder(self, queryset, name, value):  # noqa: ARG002
        """Filter by folder ID, or return unfiled records when "value" is ""none""."""
        if not value:
            return queryset

        if value == "none":
            return queryset.filter(folder__isnull=True)

        return queryset.filter(folder_id=value)

    def filter_expiring_soon(self, queryset, name, value):  # noqa: ARG002
        """Filter to records expiring within 30 days of today."""
        if value:
            today = timezone.now().date()
            return queryset.filter(
                expiry_date__lte=today + timezone.timedelta(days=30),
                expiry_date__gte=today,
            )
        return queryset

    def filter_this_month(self, queryset, name, value):  # noqa: ARG002
        """Filter to records whose transaction date falls in the current calendar month.

        Uses explicit date boundaries instead of "__month"/"__year" lookups
        so that PostgreSQL can leverage the B-tree index on "transaction_date".
        """
        if value:
            now = timezone.now()
            month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if month_start.month == 12:
                month_end = month_start.replace(year=month_start.year + 1, month=1)
            else:
                month_end = month_start.replace(month=month_start.month + 1)
            return queryset.filter(
                transaction_date__gte=month_start,
                transaction_date__lt=month_end,
            )
        return queryset

    def filter_merged(self, queryset, name, value):  # noqa: ARG002
        """Filter to records that are the Plaid side of an active merge.

        Matches via explicit merge-log IDs instead of a "merge_logs_as_plaid"
        join: filtering on a nullable related field's "isnull" also matches
        records with no merge logs at all (the LEFT JOIN emits NULLs), which
        would surface every Plaid transaction as "merged".
        """
        if value:
            merged_plaid_ids = MergeLog.objects.filter(
                plaid_record__user=self.request.user,
                undone_at__isnull=True,
            ).values_list("plaid_record_id", flat=True)
            return queryset.filter(pk__in=merged_plaid_ids)
        return queryset

    def filter_shared(self, queryset, name, value):  # noqa: ARG002
        """Filter to records in any share involving the user, either direction.

        Covers records shared "with" the user (they are the recipient) and
        records the user shared "to" others (they are the grantor), matching
        the "All Shared" summary metric. Uses "shared_by" from the share row
        so the owner matters, and "distinct()" for recipient-with-many-shares.
        """
        if value:
            user = self.request.user
            return queryset.filter(Q(shares__user=user) | Q(shares__shared_by=user)).distinct()
        return queryset
