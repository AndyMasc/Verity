"""Record sharing views: grant, revoke, bulk grant, and the management partial.

Sharing endpoints are gated on the "RECORD_SHARING" billing feature for
the "granting" user; recipients' access is defined purely by the share row.
"""

import json

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from billing.entitlements import has_feature
from billing.features import RECORD_SHARING
from Papertrail.views import parse_record_ids
from records.models import Record, RecordShare

from .. import shares as share_services

User = get_user_model()


def _can_grant_shares(user) -> bool:
    return has_feature(user, RECORD_SHARING)


class RecordSharingSectionView(LoginRequiredMixin, View):
    """Render the share management partial for a record (HTMX).

    Anyone who can see the record may see who it is shared with; only the
    owner sees grant/revoke controls.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        record = get_object_or_404(Record.objects.visible_to(request.user), pk=pk)
        context = {
            "record": record,
            "shares": share_services.shares_for_viewer(record=record, viewer=request.user),
            "can_grant": _can_grant_shares(request.user) and record.user_id == request.user.pk,
            "can_share_feature": _can_grant_shares(request.user),
            "is_recipient": record.user_id != request.user.pk
            and RecordShare.objects.filter(record=record, user=request.user).exists(),
        }
        return render(request, "records/partials/shares/share_panel.html", context)


class ShareRecordView(LoginRequiredMixin, View):
    """Grant record access to users by email (owner only, Pro gated)."""

    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        record = get_object_or_404(Record.objects.visible_to(request.user), pk=pk)
        if not _can_grant_shares(request.user):
            messages.error(request, "Record sharing requires the Pro plan")
            return redirect("records:record_detail", pk=pk)
        if record.user_id != request.user.pk:
            messages.error(request, "Only the record owner can share it")
            return redirect("records:record_detail", pk=pk)

        raw = request.POST.get("emails", "")
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        permission = request.POST.get("permission", RecordShare.Permission.EDIT)
        if permission not in RecordShare.Permission.values:
            permission = RecordShare.Permission.EDIT
        include_documents = request.POST.get("include_documents") in {
            "on",
            "true",
            "1",
        }
        try:
            shares, unknown = share_services.share_record_with_users(
                record=record,
                owner=request.user,
                emails=emails,
                permission=permission,
                include_documents=include_documents,
            )
        except share_services.SelfShare as exc:
            messages.error(request, str(exc))
        except share_services.NotOwner as exc:
            messages.error(request, str(exc))
        else:
            if shares:
                messages.success(
                    request, f"Shared with {len(shares)} user{'s' if len(shares) != 1 else ''}"
                )
            if unknown:
                messages.warning(
                    request,
                    "No account found for: " + ", ".join(unknown),
                )
        return redirect("records:record_detail", pk=pk)


@method_decorator(require_POST, name="dispatch")
class BulkShareView(LoginRequiredMixin, View):
    """Sharing several selected records at once via email (owner only, Pro gated).

    Accepts a JSON body with "{"record_ids": [1, 2, 3], "emails": "a@x.com, b@y.com"}"
    and shares every owned record with the listed recipients. Returns a JSON
    summary so the bulk action bar can surface counts via toast.
    """

    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request: HttpRequest) -> HttpResponse:
        if not _can_grant_shares(request.user):
            return JsonResponse({"error": "Record sharing requires the Pro plan"}, status=403)

        record_ids, error = parse_record_ids(request)
        if error:
            return error

        try:
            raw = json.loads(request.body or b"{}").get("emails", "")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request body."}, status=400)
        emails = [e.strip() for e in raw.split(",") if e.strip()]
        if not emails:
            return JsonResponse({"error": "At least one recipient email is required."}, status=400)

        owned = (
            Record.objects.filter(pk__in=record_ids, user=request.user)
            .only("pk", "user_id")
            .distinct()
        )
        if not owned:
            return JsonResponse(
                {"error": "None of the selected records can be shared."}, status=403
            )

        recipients, unknown = share_services.resolve_recipients(emails)
        total_shares = 0
        for record in owned:
            try:
                shares, _ = share_services.share_record_with_users(
                    record=record,
                    owner=request.user,
                    emails=emails,
                    recipients=recipients,
                )
            except share_services.SelfShare:
                continue
            total_shares += len(shares)

        return JsonResponse(
            {
                "success": True,
                "shared": total_shares,
                "records": len(owned),
                "unknown": sorted(unknown),
            }
        )


class RevokeShareView(LoginRequiredMixin, View):
    """Revoke a record share (owner only)."""

    @method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True))
    def post(self, request: HttpRequest, pk: int, share_pk: int) -> HttpResponse:
        record = get_object_or_404(Record.objects.visible_to(request.user), pk=pk)
        share = get_object_or_404(RecordShare, pk=share_pk, record=record)
        try:
            share_services.revoke_share(record=record, actor=request.user, share=share)
            messages.success(request, f"Access revoked for {share.user.email}")
        except share_services.NotOwner as exc:
            messages.error(request, str(exc))
        return redirect("records:record_detail", pk=pk)
