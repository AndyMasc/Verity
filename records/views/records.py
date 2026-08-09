"""Record list, detail, and hard-delete views."""

import json
from datetime import timedelta
from typing import Any

import posthog
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic.base import View
from django.views.generic.edit import UpdateView
from django_filters.views import FilterView
from django_ratelimit.decorators import ratelimit

from Papertrail.views import CachedPaginatorMixin, htmx_response

from .. import services
from ..filters import RecordFilter
from ..forms import RecordUpdateForm
from ..models import MergeLog, Record


def snapshot_with_currency(snapshot: dict[str, Any] | None, fallback: str) -> dict[str, Any]:
    """Ensure a MergeLog snapshot dict carries a ``currency`` key for templates.

    Snapshots created before currency was recorded lack the key; the detail
    and history templates use it as a ``currency_format`` filter argument,
    which raises ``VariableDoesNotExist`` when absent.
    """
    clean = dict(snapshot or {})
    clean.setdefault("currency", fallback)
    return clean


def _record_list_url(request, **kwargs: Any) -> str:
    """Build a records list URL that merges *kwargs* into the current query params."""
    params = request.GET.copy()
    for key, value in kwargs.items():
        if value is None:
            params.pop(key, None)
        else:
            params[key] = str(value)
    base_url = reverse("records:view_all_records")
    if params:
        return f"{base_url}?{params.urlencode()}"
    return base_url


LIST_FIELDS = (
    "pk",
    "is_active",
    "record_type",
    "title",
    "merchant",
    "expiry_date",
    "transaction_date",
    "date_added",
    "balance",
    "currency",
    "last_edited",
    "payment_method",
    "nickname",
    "notes",
)

DEFERRED_FIELDS = ("products",)


class RecordListView(LoginRequiredMixin, CachedPaginatorMixin, FilterView):
    """Paginated, filterable list of the current user's records.

    Supports ``smart_search`` via the ``search`` query param and HTMX
    partial rendering for in-page updates. Uses ``CachedPaginator`` to
    avoid re-evaluating the queryset on repeated page requests.
    """

    model = Record
    template_name = "records/record_list_view.html"
    context_object_name = "records"
    filterset_class = RecordFilter
    paginate_by = settings.PAGINATE_BY

    @method_decorator(ratelimit(key="user", rate="120/m", method="GET", block=True))
    def dispatch(self, *args, **kwargs):
        return super().dispatch(*args, **kwargs)

    def get_queryset(self):
        qs = Record.objects.visible_to(self.request.user)
        # Archived records are only reachable via the explicit "is_active" filter
        # (the sidebar Archive link, the Active/All chips). Without a filter the
        # list shows active records only, so archiving visibly removes a record.
        if "is_active" not in self.request.GET:
            qs = qs.filter(is_active=True)
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            qs = qs.smart_search(search_query)
            # Also match merged records via their merged receipt's metadata so searching for a receipt title/merchant surfaces the merged entry.
            merged_plaid_ids = MergeLog.objects.filter(
                plaid_record__user=self.request.user,
                undone_at__isnull=True,
                search_text__icontains=search_query,
            ).values_list("plaid_record_id", flat=True)
            if merged_plaid_ids:
                qs = qs | Record.objects.visible_to(self.request.user).filter(
                    pk__in=merged_plaid_ids
                )

        self.filterset = self.filterset_class(self.request.GET, queryset=qs, request=self.request)
        return self.filterset.qs.order_by("-last_edited").only(*LIST_FIELDS)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        now = timezone.now().date()
        expiring_cutoff = now + timedelta(days=30)
        base_qs = Record.objects.visible_to(self.request.user)

        metrics = base_qs.aggregate(
            total_count=Count("id", distinct=True),
            active_count=Count("id", distinct=True, filter=Q(is_active=True)),
            expiring_count=Count(
                "id",
                distinct=True,
                filter=Q(expiry_date__gte=now, expiry_date__lte=expiring_cutoff)
                & Q(is_active=True),
            ),
            inactive_count=Count("id", distinct=True, filter=Q(is_active=False)),
            shared_count=Count(
                "id",
                distinct=True,
                filter=(Q(shares__user=self.request.user) | Q(shares__shared_by=self.request.user))
                & Q(is_active=True),
            ),
        )

        merged_plaid_ids = MergeLog.objects.filter(
            plaid_record__user=self.request.user,
            undone_at__isnull=True,
        ).values_list("plaid_record_id", flat=True)
        metrics["merged_count"] = base_qs.filter(pk__in=merged_plaid_ids, is_active=True).count()

        # Counts above are active-only (they must match the default list, which
        # hides archived records). The "All Records" card deliberately counts
        # everything and links with an explicit is_active="" so the list shows
        # all records, archived included.
        all_records_url = _record_list_url(self.request, is_active="", merged=None, shared=None)
        context["metrics"] = [
            {
                "label": "All Records",
                "value": metrics["total_count"],
                "url": all_records_url,
            },
            {
                "label": "Active",
                "value": metrics["active_count"],
                "url": _record_list_url(self.request, is_active="True"),
            },
            {
                "label": "Expiring Soon",
                "value": metrics["expiring_count"],
                "url": _record_list_url(self.request, expiring_soon="True"),
            },
            {
                "label": "Inactive",
                "value": metrics["inactive_count"],
                "url": _record_list_url(self.request, is_active="False"),
            },
            {
                "label": "Merged",
                "value": metrics["merged_count"],
                "subtext": "match" if metrics["merged_count"] == 1 else "matches",
                "url": _record_list_url(self.request, merged="True"),
            },
            {
                "label": "All Shared",
                "value": metrics["shared_count"],
                "subtext": None,
                "url": _record_list_url(self.request, shared="True"),
            },
        ]

        context.update(metrics)
        context["filter"] = self.filterset
        return context

    def get_template_names(self):
        if self.request.headers.get("HX-Target") == "query-results-container":
            return ["records/partials/record_list_partial.html"]
        return [self.template_name]


class RecordDetailView(LoginRequiredMixin, UpdateView):
    """Detail view that doubles as an inline edit form for a single record.

    When accessed via HTMX, returns the form partial for in-place editing.
    Otherwise renders the full detail page with merge context when the
    record is a Plaid transaction.
    """

    template_name = "records/record_detail_view.html"
    form_class = RecordUpdateForm
    model = Record
    pk_url_kwarg = "pk"
    context_object_name = "record"

    def get_template_names(self):
        if self.request.headers.get("HX-Request") == "true":
            return ["records/partials/record_form_partial.html"]
        return [self.template_name]

    def get_queryset(self):
        return Record.objects.visible_to(self.request.user).with_documents()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        seven_years_ago = timezone.now() - timedelta(days=365 * 7)
        context["seven_years_ago_unix"] = seven_years_ago.timestamp()

        if self.object.is_plaid_record:
            active_merge = (
                MergeLog.objects.filter(
                    plaid_record=self.object,
                    undone_at__isnull=True,
                )
                .select_related("document_record", "document")
                .first()
            )
            if active_merge:
                context["active_merge"] = active_merge
                context["plaid_snapshot"] = snapshot_with_currency(
                    active_merge.plaid_snapshot, self.object.currency
                )
                context["document_snapshot"] = snapshot_with_currency(
                    active_merge.document_snapshot, self.object.currency
                )

        return context

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, "Record updated successfully.")
        self.object = form.save()

        posthog.capture(
            "record_updated",
            distinct_id=str(self.request.user.pk),
            properties={
                "record_type": self.object.record_type,
            },
        )

        resp = htmx_response(self.request, toast="Record updated successfully.")
        if resp is not None:
            return resp

        return redirect("records:record_detail", pk=self.object.pk)

    def form_invalid(self, form):
        messages.error(self.request, "An error was left in a record")
        is_htmx = self.request.headers.get("HX-Request") == "true"
        response = render(
            self.request,
            self.get_template_names(),
            self.get_context_data(form=form),
            status=200 if is_htmx else 422,
        )
        if is_htmx:
            response["HX-Trigger"] = json.dumps(
                {"showToast": {"text": "An error was left in a record", "tags": "error"}}
            )
        return response


class HardDeleteRecordView(LoginRequiredMixin, View):
    """Permanently delete a record that is at least seven years old.

    Hard-deletes are irreversible and also remove associated DocumentData
    and S3 files. Rate-limited to 5 POSTs per minute per user.
    """

    @method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True))
    def post(self, request, pk: int) -> HttpResponse:
        record = get_object_or_404(Record, pk=pk, user=request.user)
        seven_years_ago = timezone.now() - timedelta(days=365 * 7)
        if record.date_added > seven_years_ago.date():
            resp = htmx_response(
                request,
                toast="This record is not old enough for permanent deletion.",
                toast_tags="error",
                status=409,
            )
            if resp is not None:
                return resp
            messages.error(request, "This record is not old enough for permanent deletion.")
            return redirect("records:record_detail", pk=pk)

        services.hard_delete_record(request.user, record)

        posthog.capture(
            "record_hard_deleted",
            distinct_id=str(request.user.pk),
            properties={
                "record_type": record.record_type,
            },
        )

        resp = htmx_response(
            request,
            toast="Record permanently deleted.",
            redirect_url=reverse("records:view_all_records"),
        )
        if resp is not None:
            return resp
        messages.success(request, "Record permanently deleted.")
        return redirect("records:view_all_records")
