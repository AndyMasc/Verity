"""Document detail and delete views."""

import json
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import UpdateView
from django_ratelimit.decorators import ratelimit

from records.models import Record

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
        return DocumentData.objects.filter(
            Q(user=self.request.user)
            | Q(associated_record__in=Record.objects.visible_to(self.request.user))
        ).select_related("associated_record")

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        ctx = DocumentDetailService.build_context(self.object, self.request)
        context["view_url"] = ctx.view_url
        context["records"] = ctx.records
        context["page_obj"] = ctx.page_obj
        context["is_paginated"] = ctx.is_paginated
        return context

    @transaction.atomic
    def form_valid(self, form) -> HttpResponse:
        if self.object.user_id != self.request.user.pk:
            # Documents attached to records shared with you are view-only.
            if self.request.headers.get("HX-Request") == "true":
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps(
                    {
                        "showToast": {
                            "text": "Documents on shared records are view-only.",
                            "tags": "error",
                        }
                    }
                )
                return response
            return HttpResponseForbidden("This document is view-only.")
        if "associated_record" in self.request.POST:
            record_id = self.request.POST.get("associated_record", "").strip()
            DocumentDetailService.associate_record(form.instance, record_id, self.request.user)

        form.save()

        if self.request.headers.get("HX-Request") == "true":
            if "associated_record" in self.request.POST:
                redirect_url = reverse("documents:view_document", kwargs={"pk": self.object.pk})
                response = HttpResponse(status=204)
                response["HX-Redirect"] = redirect_url
                return response
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps({"recordChanged": {}, "documentChanged": {}})
            return response

        messages.success(self.request, "Updated successfully.")
        return redirect("documents:view_document", pk=self.object.pk)

    def form_invalid(self, form) -> HttpResponse:
        messages.error(self.request, "An error occurred.")
        if self.request.headers.get("HX-Request") == "true":
            return self.render_to_response(self.get_context_data(form=form), status=422)
        return super().form_invalid(form)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="dispatch")
class DeleteDocument(LoginRequiredMixin, View):
    """Permanently deletes a document and redirects to the parent record."""

    def post(self, request: HttpRequest, document_id: int) -> HttpResponse:
        document = get_object_or_404(
            DocumentData.objects.filter(user=request.user).with_record(),
            pk=document_id,
        )
        record = document.associated_record
        result = DocumentDeletionService.soft_delete(document)

        if not result.success:
            if request.headers.get("HX-Request") == "true":
                response = HttpResponse(status=204)
                response["HX-Trigger"] = json.dumps(
                    {
                        "recordChanged": {},
                        "documentChanged": {},
                        "showToast": {
                            "text": result.error or "An error occurred.",
                            "tags": "error",
                        },
                    }
                )
                return response
            messages.error(request, result.error or "An error occurred.")
            return redirect(
                reverse("records:record_detail", kwargs={"pk": record.id})
                if record
                else reverse("records:view_all_records")
            )

        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps(
                {
                    "recordChanged": {},
                    "documentChanged": {},
                    "showToast": {
                        "text": result.message,
                        "tags": result.message_tag or "success",
                    },
                }
            )
            return response
        messages.success(request, result.message)
        url = (
            reverse("records:record_detail", kwargs={"pk": record.id})
            if record
            else reverse("records:view_all_records")
        )
        return redirect(url)
