"""Record creation views with OCR integration."""

import logging

import posthog
from cachalot.api import cachalot_disabled
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.functional import cached_property
from django.views.generic.base import View
from django.views.generic.edit import CreateView

from documents.models import DocumentData, DocumentStatus

from .. import services
from ..forms import AddRecordForm
from ..matching import try_match_document_record

logger = logging.getLogger(__name__)

_PROCESSING_STATUSES = (
    DocumentStatus.UPLOADED,
    DocumentStatus.PENDING_UPLOAD,
    DocumentStatus.PROCESSING,
)


def _ocr_error_message(document: DocumentData) -> str:
    """Return a user-facing error message for a failed OCR document."""
    data = cache.get(f"ocr_status_{document.id}")
    if isinstance(data, dict) and data.get("error"):
        return data["error"]
    return "Extraction failed. Please enter the details manually."


class AddRecordView(LoginRequiredMixin, CreateView):
    """Create a new record, either manually or via an uploaded document.

    When a "document_id" is provided, this view shows a waiting spinner
    while background processing completes, redirects to the record detail
    once available, or shows an error card on failure.
    """

    template_name = "records/add_record.html"
    form_class = AddRecordForm

    @cached_property
    def document(self) -> DocumentData | None:
        document_id = self.kwargs.get("document_id")
        if not document_id:
            return None
        with cachalot_disabled():
            return get_object_or_404(
                DocumentData.objects.select_related("associated_record"),
                id=document_id,
                user=self.request.user,
            )

    def get(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        document = self.document
        if document:
            if document.associated_record:
                return redirect("records:record_detail", pk=document.associated_record.pk)

            if document.status == DocumentStatus.COMPLETED:
                record = services.create_record_from_ocr(document.id)
                if record:
                    return redirect("records:record_detail", pk=record.pk)

        return super().get(request, *args, **kwargs)

    def get_form_kwargs(self) -> dict:
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs) -> dict:
        context = super().get_context_data(**kwargs)
        document = self.document

        is_waiting = False
        error_message = None
        form = context.get("form") or self.get_form()

        if document:
            if document.status in _PROCESSING_STATUSES:
                is_waiting = True
            elif document.status == DocumentStatus.ERROR:
                error_message = _ocr_error_message(document)
            else:
                error_message = "Extraction produced no data. Please enter details manually."

        context.update(
            {
                "form": form,
                "document": document,
                "document_id": self.kwargs.get("document_id"),
                "is_waiting": is_waiting,
                "error_message": error_message,
            }
        )
        return context

    @transaction.atomic
    def form_valid(self, form) -> HttpResponse:
        document = self.document

        if document and document.associated_record:
            return redirect("records:record_detail", pk=document.associated_record.pk)

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

        return redirect("records:record_detail", pk=self.object.pk)


class CheckOCRStatus(LoginRequiredMixin, View):
    """HTMX endpoint that polls the OCR status of a document.

    Returns a waiting spinner while processing, an HX-Redirect to the
    auto-created record's detail page on success, or an error card on failure.
    """

    def get(self, request: HttpRequest, document_id: int) -> HttpResponse:
        with cachalot_disabled():
            document = DocumentData.objects.filter(id=document_id, user=request.user).first()
            if not document:
                raise Http404("Document not found.")

            if document.status == DocumentStatus.COMPLETED:
                record = services.create_record_from_ocr(document.id)
                if record:
                    response = HttpResponse(status=200)
                    response["HX-Redirect"] = reverse(
                        "records:record_detail", kwargs={"pk": record.pk}
                    )
                    return response
                return render(
                    request,
                    "records/partials/form_card.html",
                    {
                        "is_waiting": False,
                        "error_message": "Extraction produced no data. Please enter details manually.",
                    },
                )
            elif document.status == DocumentStatus.ERROR:
                return render(
                    request,
                    "records/partials/form_card.html",
                    {
                        "is_waiting": False,
                        "error_message": _ocr_error_message(document),
                    },
                )

        return render(
            request,
            "records/partials/form_card.html",
            {"is_waiting": True, "document_id": document_id},
        )
