"""Record sharing views: grant, revoke, bulk grant, and the management partial.

Sharing endpoints are gated on the "RECORD_SHARING" billing feature for
the "granting" user; recipients' access is defined purely by the share row.
"""

import json

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from billing.entitlements import has_feature
from core.apps import posthog_client
from records.models import Record, RecordShare
from Verity.views import parse_record_ids

from .. import shares as share_services


def _owned_record_or_404(request: HttpRequest, pk: int) -> Record:
    """Fetch a record the requester may see, for owner-gated actions."""
    return get_object_or_404(Record.objects.visible_to(request.user), pk=pk)


class RecordSharingSectionView(LoginRequiredMixin, View):
    """Render the share management partial for a record (HTMX).

    Anyone who can see the record may see who it is shared with; only the
    owner sees grant/revoke controls.
    """

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        record = _owned_record_or_404(request, pk)
        is_owner = record.user_id == request.user.pk
        has_share_feature = has_feature(request.user, "record-sharing")
        context = {
            "record": record,
            "shares": share_services.shares_for_viewer(record=record, viewer=request.user),
            "can_grant": has_share_feature and is_owner,
            "can_share_feature": has_share_feature,
            "is_recipient": not is_owner
            and RecordShare.objects.filter(record=record, user=request.user).exists(),
        }
        return render(request, "records/partials/shares/share_panel.html", context)


@method_decorator(require_POST, name="dispatch")
class BulkShareView(LoginRequiredMixin, View):
    """Share selected records at once by email (owner only, Pro gated).

    The single path for granting access: sharing one record is just this with
    one id. Takes a JSON body of
    "{"record_ids": [1, 2], "emails": "a@x.com, b@y.com", "permission": "view"}"
    and returns a JSON summary for the bulk action bar.
    """

    @method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True))
    def post(self, request: HttpRequest) -> HttpResponse:
        if not has_feature(request.user, "record-sharing"):
            return JsonResponse({"error": "Record sharing requires the Pro plan"}, status=403)

        record_ids, error = parse_record_ids(request)
        if error:
            return error

        try:
            body = json.loads(request.body or b"{}")
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid request body."}, status=400)

        emails = [e.strip() for e in body.get("emails", "").split(",") if e.strip()]
        if not emails:
            return JsonResponse({"error": "At least one recipient email is required."}, status=400)

        permission = body.get("permission", RecordShare.Permission.EDIT)
        if permission not in RecordShare.Permission.values:
            permission = RecordShare.Permission.EDIT
        include_documents = bool(body.get("include_documents", True))
        config = share_services.ShareConfig(
            permission=permission,
            include_documents=include_documents,
        )

        owned = list(Record.objects.filter(pk__in=record_ids, user=request.user).distinct())
        if not owned:
            return JsonResponse(
                {"error": "None of the selected records can be shared."}, status=403
            )

        # Resolve once for the whole batch, and drop the owner up front: they
        # already have access, and leaving them in would be a no-op at best.
        found, unknown = share_services.resolve_recipients(emails)
        recipients = [u for u in found if u.pk != request.user.pk]
        self_addressed = len(found) != len(recipients)

        granted_shares: list[RecordShare] = []
        for record in owned:
            granted_shares.extend(
                share_services.grant_shares(
                    record=record,
                    owner=request.user,
                    recipients=recipients,
                    config=config,
                )
            )

        # One notification per recipient covering everything they were granted,
        # rather than one email per record.
        share_services.notify_share_recipients(shares=granted_shares, actor=request.user)

        total_shares = len(granted_shares)

        if total_shares and posthog_client is not None:
            # One event whatever the size, with record_count carrying how many
            # went out, so single and bulk shares are one signal to compare.
            posthog_client.capture(
                "record_shared",
                properties={
                    "record_count": len(owned),
                    "recipient_count": len(recipients),
                    "permission": permission,
                    "includes_documents": include_documents,
                },
            )

        return JsonResponse(
            {
                "success": True,
                "shared": total_shares,
                "records": len(owned),
                "recipients": len(recipients),
                "unknown": sorted(unknown),
                "self_skipped": len(owned) if self_addressed else 0,
            }
        )


class RevokeShareView(LoginRequiredMixin, View):
    """Revoke a record share (owner only)."""

    @method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True))
    def post(self, request: HttpRequest, pk: int, share_pk: int) -> HttpResponse:
        record = _owned_record_or_404(request, pk)
        share = get_object_or_404(RecordShare, pk=share_pk, record=record)
        try:
            share_services.revoke_share(record=record, actor=request.user, share=share)
        except share_services.NotOwnerError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"Access revoked for {share.user.email}")
        return redirect("records:record_detail", pk=pk)
