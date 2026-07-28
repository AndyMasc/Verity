"""Record list, detail, and hard-delete views."""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic.base import View
from django.views.generic.edit import UpdateView
from django_filters.views import FilterView
from django_ratelimit.decorators import ratelimit

from core.services.dashboard import invalidate_dashboard_cache
from Papertrail.views import CachedPaginatorMixin, htmx_response

from ..filters import RecordFilter
from ..forms import RecordUpdateForm
from ..models import AuditLog, MergeLog, Record

logger = logging.getLogger(__name__)

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
        qs = Record.objects.for_user(self.request.user).only(*LIST_FIELDS)
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            return qs.smart_search(search_query)
        return qs.order_by("-last_edited")

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
        return Record.objects.for_user(self.request.user).with_documents()

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
                context["plaid_snapshot"] = active_merge.plaid_snapshot
                context["document_snapshot"] = active_merge.document_snapshot

        return context

    @transaction.atomic
    def form_valid(self, form):
        messages.success(self.request, "Record updated successfully.")
        self.object = form.save()
        invalidate_dashboard_cache(self.request.user.id)

        if self.request.headers.get("HX-Request") == "true":
            response = render(
                self.request,
                self.get_template_names(),
                self.get_context_data(form=form),
                status=200,
            )
            response["HX-Trigger"] = json.dumps({"recordChanged": {}})
            return response

        return redirect("records:record_detail", pk=self.object.pk)

    def form_invalid(self, form):
        messages.error(self.request, "An error was left in a record")
        return render(
            self.request,
            self.get_template_names(),
            self.get_context_data(form=form),
            status=422,
        )


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

        from documents.models import DocumentData

        with transaction.atomic():
            for doc in DocumentData.objects.filter(associated_record=record):
                doc.hard_delete()
            AuditLog.objects.create(
                user=request.user,
                action=AuditLog.Action.HARD_DELETE,
                record=record,
                details={"title": record.title},
            )
            record.hard_delete()

        invalidate_dashboard_cache(request.user.id)
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "recordChanged": {},
                    "showToast": {"text": "Record permanently deleted.", "tags": "success"},
                    "HX-Redirect": reverse("records:view_all_records"),
                }
            )
            return response
        messages.success(request, "Record permanently deleted.")
        return redirect("records:view_all_records")
