"""Upload views: presigned R2 uploads, confirmation, and supporting document flow."""

import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from records.models import Record

from ..models import DocumentData, DocumentStatus
from ..services import ConfirmUploadService, UploadService

logger = logging.getLogger(__name__)


def _presign_to_json(result) -> JsonResponse:
    """Convert a PresignResult dataclass to a JsonResponse."""
    if result.status == "error":
        body = {"error": result.error}
        if result.error_details:
            body["details"] = result.error_details
        return JsonResponse(body, status=400)
    if result.status == "duplicate_confirmed":
        return JsonResponse(
            {
                "status": result.status,
                "document_id": result.existing_document_id,
                "record_id": result.existing_record_id,
                "record_label": result.existing_record_label,
                "record_url": result.existing_record_url,
            }
        )
    return JsonResponse(
        {
            "status": result.status,
            "upload_url": result.upload_url,
            "key": result.key,
            "document_id": result.document_id,
        }
    )


class BaseR2UploadView(LoginRequiredMixin, View):
    """Shared presigned-URL upload logic used by primary and supporting upload flows."""

    @method_decorator(ratelimit(key="user", rate="30/h", method="POST", block=True))
    def dispatch(self, *args: Any, **kwargs: Any) -> HttpResponse:
        return super().dispatch(*args, **kwargs)

    def _handle_presign_request(
        self, request: HttpRequest, record_id: int | None = None
    ) -> JsonResponse:
        """Delegate to UploadService and convert its result to JSON."""
        service = UploadService(request, record_id=record_id)
        result = service.handle()
        return _presign_to_json(result)


class UploadView(BaseR2UploadView):
    """Primary document upload page and presign endpoint."""

    def get(self, request: HttpRequest) -> HttpResponse:
        context = {
            "page_title": "Upload a financial record.",
            "page_subtitle": "We'll extract and organize the details automatically.",
            "api_url": reverse("documents:upload_document"),
            "redirect_url_template": reverse("records:add_record", kwargs={"document_id": "0"}),
            "is_supporting_flow": False,
        }
        return render(request, "documents/upload_file.html", context)

    def post(self, request: HttpRequest) -> JsonResponse:
        return self._handle_presign_request(request)


class ConfirmUploadView(LoginRequiredMixin, View):
    """Confirms a completed R2 upload, runs gatekeeper validation, and transitions status."""

    @method_decorator(ratelimit(key="user", rate="60/h", method="POST", block=True))
    def dispatch(self, *args: Any, **kwargs: Any) -> HttpResponse:
        return super().dispatch(*args, **kwargs)

    def post(self, request: HttpRequest) -> JsonResponse:
        document_id = request.POST.get("document_id")
        key = request.POST.get("key", "").strip()

        if not document_id or not key:
            return JsonResponse({"error": "Missing document_id or key."}, status=400)

        with transaction.atomic():
            try:
                document = DocumentData.objects.select_for_update().get(
                    id=document_id,
                    user=request.user,
                )
            except DocumentData.DoesNotExist:
                return JsonResponse({"error": "Document not found."}, status=404)

            if document.status != DocumentStatus.PENDING_UPLOAD:
                return JsonResponse(
                    {"error": f"Unexpected status: {document.status}."},
                    status=409,
                )

            service = ConfirmUploadService(document=document, key=key)
            result = service.confirm()

            if not result.valid:
                return JsonResponse({"error": result.error}, status=result.status_code)

        return JsonResponse({"status": "confirmed", "document_id": document.id})


class AddSupportDocuments(BaseR2UploadView):
    """Upload flow for attaching supporting documents to an existing record."""

    def get(self, request: HttpRequest, record_id: int) -> HttpResponse:
        record = get_object_or_404(Record, pk=record_id, user=request.user)
        context = {
            "record": record,
            "page_title": "Add supporting documents.",
            "page_subtitle": f"Attach additional records directly to {record.title}.",
            "api_url": reverse("documents:add_support_docs", kwargs={"record_id": record_id}),
            "redirect_url_template": f"/records/record_detail/{record_id}",
            "is_supporting_flow": True,
        }
        return render(request, "documents/upload_supporting_files.html", context)

    def post(self, request: HttpRequest, record_id: int) -> JsonResponse:
        get_object_or_404(Record, pk=record_id, user=request.user)
        return self._handle_presign_request(request, record_id=record_id)
