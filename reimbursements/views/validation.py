import json

from django.contrib.auth import get_user_model
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit


@require_POST
@ratelimit(key="user", rate="30/m", method="POST", block=True)
def validate_recipient_email(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "error": "Invalid request."}, status=400)

    email = data.get("email", "").strip()
    if not email:
        return JsonResponse({"valid": False, "error": "Email is required."}, status=400)

    user_model = get_user_model()
    try:
        recipient = user_model.objects.get(email__iexact=email)
        if recipient == request.user:
            return JsonResponse({"valid": False, "error": "You cannot send a package to yourself."})
        return JsonResponse({"valid": True, "name": recipient.get_full_name() or recipient.email})
    except user_model.DoesNotExist:
        return JsonResponse(
            {"valid": False, "error": "No Papertrail user found with that email address."}
        )
