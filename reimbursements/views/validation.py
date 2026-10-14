import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit


@require_POST
@login_required
@ratelimit(key="user", rate="30/m", method="POST", block=True)
def validate_recipient_email(request: HttpRequest) -> JsonResponse:
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "error": "Invalid request."}, status=400)

    email = data.get("email", "").strip()
    if not email:
        return JsonResponse({"valid": False, "error": "Email is required."}, status=400)

    if email.lower() == request.user.email.lower():
        return JsonResponse({"valid": False, "error": "You cannot send a package to yourself."})

    user_model = get_user_model()
    recipient = user_model.objects.filter(email__iexact=email).exists()
    return JsonResponse({"valid": True, "registered": recipient})
