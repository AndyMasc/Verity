"""Document detail, delete, undo-delete, and hard-delete views."""

from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.messages import constants as message_constants
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import UpdateView
from django_ratelimit.decorators import ratelimit

from Papertrail.views import htmx_response

from ..forms import DocumentUpdateForm
from ..models import DocumentData
from ..services import DocumentDeletionService, DocumentDetailService


class ViewDocument(LoginRequiredMixin, UpdateView):
    """Document detail page with metadata editing and record association."""

    model = DocumentData
    form_class = DocumentUpdateForm
    template_name = "documents/view_document.html"
    context_object_name = "document"

    def get_template_names(self) -> list[str]:
        if self.request.headers.get("HX-Target") in [
            "search-results",
            "query-results-container",
        ]:
            return ["documents/partials/record_list_partial.html"]
        if self.request.headers.get("HX-Target") in [
            "document-form-container",
            "document-metadata-form",
        ]:
            return ["documents/partials/document_form_partial.html"]
        return [self.template_name]

    def get_queryset(self):
        return DocumentData.objects.filter(user=self.request.user)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ctx = DocumentDetailService.build_context(self.object, self.request)
        context["view_url"] = ctx.view_url
        context["seven_years_ago_unix"] = ctx.seven_years_ago_unix
        context["records"] = ctx.records
        context["page_obj"] = ctx.page_obj
        context["is_paginated"] = ctx.is_paginated
        return context

    @transaction.atomic
    def form_valid(self, form) -> HttpResponse:
        if "associated_record" in self.request.POST:
            record_id = self.request.POST.get("associated_record", "").strip()
            DocumentDetailService.associate_record(form.instance, record_id, self.request.user)

        form.save()
        messages.success(self.request, "Updated successfully.")

        if self.request.headers.get("HX-Request") == "true":
            if "associated_record" in self.request.POST:
                redirect_url = reverse("documents:view_document", kwargs={"pk": self.object.pk})
                response = HttpResponse(status=204)
                response["HX-Redirect"] = redirect_url
                return response
            return HttpResponse(status=204)

        return redirect("documents:view_document", pk=self.object.pk)

    def form_invalid(self, form) -> HttpResponse:
        messages.error(self.request, "An error occurred.")
        if self.request.headers.get("HX-Request") == "true":
            return self.render_to_response(self.get_context_data(form=form), status=422)
        return super().form_invalid(form)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="dispatch")
class DeleteDocument(LoginRequiredMixin, View):
    """Soft or hard-deletes a document and redirects to the parent record."""

    def post(self, request: HttpRequest, document_id: int) -> HttpResponse:
        document = get_object_or_404(
            DocumentData.objects.filter(user=request.user).with_record(),
            pk=document_id,
        )
        record = document.associated_record
        result = DocumentDeletionService.soft_delete(document)

        if not result.success:
            messages.error(request, result.error or "An error occurred.")
            resp = htmx_response(request)
            if resp is not None:
                resp["HX-Refresh"] = "true"
                return resp
            return redirect(
                reverse("records:record_detail", kwargs={"pk": record.id})
                if record
                else reverse("records:view_all_records")
            )

        messages.add_message(
            request,
            message_constants.SUCCESS
            if result.message_tag == "success"
            else message_constants.INFO,
            result.message,
        )
        url = (
            reverse("records:record_detail", kwargs={"pk": record.id})
            if record
            else reverse("records:view_all_records")
        )
        return redirect(url)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="dispatch")
class UndoDeleteDocument(LoginRequiredMixin, View):
    """Restores a soft-deleted document to active status."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        document = get_object_or_404(DocumentData, pk=pk, user=request.user, is_active=False)
        result = DocumentDeletionService.undo_delete(document)
        resp = htmx_response(request, toast=result.message)
        if resp is not None:
            return resp
        messages.success(request, result.message)
        return redirect("documents:trash_list")


@method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True), name="dispatch")
class HardDeleteDocumentView(LoginRequiredMixin, View):
    """Permanently deletes documents older than 7 years from R2 and the database."""

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        document = get_object_or_404(DocumentData, pk=pk, user=request.user)

        if not DocumentDeletionService.is_eligible_for_hard_delete(document):
            resp = htmx_response(
                request,
                toast="This document is not old enough for permanent deletion.",
                toast_tags="error",
            )
            if resp is not None:
                return resp
            messages.error(request, "This document is not old enough for permanent deletion.")
            return redirect("documents:view_document", pk=pk)

        result = DocumentDeletionService.hard_delete(document)
        if result.filepath:
            from ..tasks import delete_document

            delete_document(result.filepath)

        resp = htmx_response(
            request,
            toast=result.message,
            redirect_url=reverse("documents:trash_list"),
        )
        if resp is not None:
            return resp
        messages.success(request, result.message)
        return redirect("documents:trash_list")
