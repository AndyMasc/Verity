"""Record creation views with OCR integration."""

import logging

import posthog
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.functional import cached_property
from django.views.generic.base import View
from django.views.generic.edit import CreateView

from documents.models import DocumentData, DocumentStatus
from documents.ocr_helpers import ocr_data_to_form_initial
from documents.tasks import extract_document
from Papertrail.responses import api_error

from ..forms import AddRecordForm
from ..matching import try_match_document_record
from ..models import Folder

logger = logging.getLogger(__name__)


def _resolve_suggested_folder(user, initial: dict) -> dict:
    """Pop ``suggested_folder`` from *initial* and replace it with the matching Folder PK.

    OCR extraction may return a folder name string; this converts it to
    the integer primary key expected by ModelChoiceField, or removes it
    if no matching folder exists.
    """
    suggested = initial.pop("suggested_folder", None)
    if suggested:
        folder = Folder.objects.filter(user=user, name__iexact=suggested).first()
        if folder:
            initial["folder"] = folder.pk
    return initial


class AddRecordView(LoginRequiredMixin, CreateView):
    """Create a new record, optionally pre-filled from OCR data of an attached document.

    When a ``document_id`` is provided, the view polls OCR status and shows
    a loading spinner until extraction completes. On successful save, the
    system attempts an automatic merge with the best matching Plaid
    transaction.
    """

    template_name = "records/add_record.html"
    form_class = AddRecordForm

    @cached_property
    def document(self):
        document_id = self.kwargs.get("document_id")
        if not document_id:
            return None
        return get_object_or_404(
            DocumentData.objects.select_related("associated_record"),
            id=document_id,
            user=self.request.user,
        )

    def get(self, request, *args, **kwargs):
        document = self.document
        if document:
            if document.associated_record:
                return api_error(
                    request,
                    "This document is already associated with a record.",
                    code="document_already_associated",
                    status=400,
                )
            if document.status in (
                DocumentStatus.UPLOADED,
                DocumentStatus.PENDING_UPLOAD,
                DocumentStatus.PROCESSING,
            ):
                cache_key = f"ocr_status_{document.id}"
                current_cached = cache.get(cache_key)
                if current_cached is None:
                    if document.did_ocr:
                        document.did_ocr = False
                        document.save(update_fields=["did_ocr"])
                    cache.set(cache_key, "processing", timeout=600)
                    extract_document.delay(document.id)

        return super().get(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        document = self.document

        is_waiting = False
        form = context.get("form") or self.get_form()

        if document:
            cache_key = f"ocr_status_{document.id}"
            cached_status = cache.get(cache_key)

            if document.status in (
                DocumentStatus.PROCESSING,
                DocumentStatus.UPLOADED,
                DocumentStatus.PENDING_UPLOAD,
            ):
                is_waiting = True
            elif document.status == DocumentStatus.ERROR:
                error_data = cached_status if isinstance(cached_status, dict) else {}
                if isinstance(error_data, dict) and "error" in error_data:
                    context["error_message"] = error_data["error"]
                form = AddRecordForm(user=self.request.user)
            elif document.status == DocumentStatus.COMPLETED and isinstance(cached_status, dict):
                initial = ocr_data_to_form_initial(cached_status)
                _resolve_suggested_folder(self.request.user, initial)
                form = AddRecordForm(initial=initial, user=self.request.user)

        context.update(
            {
                "form": form,
                "document": document,
                "document_id": self.kwargs.get("document_id"),
                "is_waiting": is_waiting,
            }
        )
        return context

    @transaction.atomic
    def form_valid(self, form):
        document = self.document

        if document and document.associated_record:
            return api_error(
                self.request,
                "This document is already associated with a record.",
                code="document_already_associated",
                status=400,
            )

        self.object = form.save(commit=False)
        self.object.user = self.request.user
        self.object.save()
        form.save_m2m()

        if document:
            document.associated_record = self.object
            document.save(update_fields=["associated_record"])

        posthog.capture(
            "record_created",
            distinct_id=str(self.request.user.pk),
            properties={
                "record_type": self.object.record_type,
                "has_document": document is not None,
            },
        )

        merged = try_match_document_record(self.object, document) if document else None
        if merged:
            messages.success(self.request, "Receipt matched with bank transaction and merged.")
            return redirect("records:record_detail", pk=merged.pk)

        return redirect("documents:add_support_docs", record_id=self.object.pk)


class CheckOCRStatus(LoginRequiredMixin, View):
    """HTMX endpoint that polls the OCR status of a document and returns the appropriate partial.

    Returns a waiting spinner while processing, the pre-filled form when
    extraction succeeds, or an error message on failure.
    """

    def get(self, request: HttpRequest, document_id: int) -> HttpResponse:
        document = DocumentData.objects.filter(id=document_id, user=request.user).first()
        if not document:
            raise Http404("Document not found.")

        if document.status in (DocumentStatus.COMPLETED, DocumentStatus.ERROR):
            return _render_completed_form(request, document)

        return render(
            request,
            "records/partials/form_card.html",
            {"is_waiting": True, "document_id": document_id},
        )


def _render_completed_form(request: HttpRequest, document: DocumentData) -> HttpResponse:
    """Render the form partial for a completed or errored OCR document."""
    cache_key = f"ocr_status_{document.id}"
    data = cache.get(cache_key)

    if document.status == DocumentStatus.ERROR:
        error_msg = (
            data.get("error", "Extraction failed.")
            if isinstance(data, dict)
            else "Extraction failed."
        )
        return render(
            request,
            "records/partials/form_card.html",
            {
                "is_waiting": False,
                "error_message": error_msg,
                "form": AddRecordForm(user=request.user),
            },
        )

    if isinstance(data, dict) and "error" not in data:
        initial = ocr_data_to_form_initial(data)
        _resolve_suggested_folder(request.user, initial)
        form = AddRecordForm(initial=initial, user=request.user)
        return render(
            request,
            "records/partials/form_card.html",
            {"form": form, "is_waiting": False},
        )

    return render(
        request,
        "records/partials/form_card.html",
        {
            "is_waiting": False,
            "error_message": "Extraction produced no data. Please enter details manually.",
            "form": AddRecordForm(user=request.user),
        },
    )
