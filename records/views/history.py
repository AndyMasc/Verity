"""Unified history timeline view for records."""

import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404
from django.views.generic.list import ListView

from documents.models import DocumentData

from ..models import MergeLog, Record
from ..types import HistoryEntry

logger = logging.getLogger(__name__)

_HISTORY_EXCLUDE = frozenset(
    {
        "id",
        "user",
        "last_edited",
        "is_active",
        "expiry_notification_sent",
        "plaid_transaction_id",
        "plaid_item",
    }
)

_HISTORY_MAX_ENTRIES_PER_SOURCE = 200


class RecordHistoryView(LoginRequiredMixin, ListView):
    """Unified history timeline for a record, merging Record history, DocumentData history, and MergeLog entries.

    Each entry is normalised into a ``SimpleNamespace`` with ``source_type``,
    ``history_type``, and ``history_date`` so the template can render them
    in a single chronological list.
    """

    template_name = "records/record_history.html"
    context_object_name = "history"
    paginate_by = 25

    def get_queryset(self):
        from django.db.models import Q

        self._record = get_object_or_404(Record, pk=self.kwargs["pk"], user=self.request.user)

        record_entries = list(self._record.history.all()[:_HISTORY_MAX_ENTRIES_PER_SOURCE])
        for h in record_entries:
            h.source_type = "record"

        doc_ids = set(self._record.documents.values_list("pk", flat=True))
        doc_ids.update(
            v["pk"]
            for v in DocumentData.history.filter(associated_record=self._record)
            .values("pk")
            .distinct()
        )

        doc_entries = (
            list(
                DocumentData.history.filter(pk__in=doc_ids).select_related("history_user")[
                    :_HISTORY_MAX_ENTRIES_PER_SOURCE
                ]
            )
            if doc_ids
            else []
        )

        for h in doc_entries:
            h.source_type = "document"

        merged = record_entries + doc_entries

        merges = MergeLog.objects.filter(
            Q(plaid_record=self._record) | Q(document_record=self._record)
        )[:_HISTORY_MAX_ENTRIES_PER_SOURCE]
        for merge in merges:
            merged.append(
                HistoryEntry(
                    source_type="merge",
                    history_type="+",
                    history_date=merge.created_at,
                    history_user=None,
                    merge=merge,
                )
            )
            if merge.undone_at:
                merged.append(
                    HistoryEntry(
                        source_type="merge",
                        history_type="-",
                        history_date=merge.undone_at,
                        history_user=None,
                        merge=merge,
                    )
                )

        merged.sort(key=lambda x: x.history_date, reverse=True)
        return merged

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record = self._record
        context["record"] = record
        context["tracked_fields"] = tuple(
            f.name
            for f in Record._meta.get_fields()
            if f.name not in _HISTORY_EXCLUDE
            and f.name != "folder"
            and not f.auto_created
            and not getattr(f, "is_relation", False)
        )
        context["doc_tracked_fields"] = tuple(
            f.name
            for f in DocumentData._meta.get_fields()
            if f.name
            not in (
                "id",
                "user",
                "filepath",
                "file_hash",
                "file_extension",
                "file_size",
                "mime_type",
                "ocr_retries",
                "ocr_error",
                "ocr_metadata",
                "status",
                "is_active",
                "created_at",
                "updated_at",
                "deleted_at",
            )
            and not f.auto_created
            and not getattr(f, "is_relation", False)
        )
        return context
