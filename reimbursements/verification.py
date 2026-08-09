"""Email verification for external (unauthenticated) reimbursement payers.

External recipients must prove they are the intended recipient before they
can view a package or pay for it: they enter the email the package was sent
to, receive a short-lived one-time code, and enter it back. Codes are stored
hashed, expire after a few minutes, and have a limited attempt budget.
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from core.tasks import send_background_email

from .models import PackageEmailVerification, ReimbursementPackage

logger = logging.getLogger(__name__)

CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
_DIGITS = 6


def _generate_code() -> str:
    return f"{secrets.randbelow(10**_DIGITS):0{_DIGITS}d}"


def _hash_code(code: str, salt) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode()).hexdigest()


def matches_recipient(package: ReimbursementPackage, email: str) -> bool:
    recipient_address = package.recipient_address
    if not recipient_address:
        return False
    return recipient_address.lower() == email.strip().lower()


def send_verification_code(package: ReimbursementPackage, email: str) -> bool:
    """Issue and email a verification code to the given address.

    Sends only when the address matches the package's recipient, keeping the
    emailed code from being sprayed at arbitrary addresses. Returns True when
    a code was issued (whether or not delivery later fails).
    """
    if not matches_recipient(package, email):
        return False

    code = _generate_code()
    now = timezone.now()
    _verification, _ = PackageEmailVerification.objects.update_or_create(
        package=package,
        defaults={
            "email": email.strip().lower(),
            "code_hash": _hash_code(code, str(package.uuid)),
            "attempts": 0,
            "expires_at": now + CODE_TTL,
            "verified_at": None,
        },
    )

    subject = "Your Papertrail verification code"
    html_body = render_to_string(
        "reimbursements/email/verification_code_message.html",
        {"code": code, "minutes": int(CODE_TTL.total_seconds() // 60)},
    )
    text_body = render_to_string(
        "reimbursements/email/verification_code_message.txt",
        {"code": code, "minutes": int(CODE_TTL.total_seconds() // 60)},
    )
    send_background_email.send(
        subject=subject,
        message=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email.strip().lower()],
        html_message=html_body,
    )
    logger.info("Issued verification code for package %s", package.uuid)
    return True


def verify_code(package: ReimbursementPackage, email: str, code: str) -> tuple[bool, str | None]:
    """Validate a submitted code against the latest issued one.

    Returns "(True, None)" on success or "(False, user-facing error)".
    Failed attempts count toward a per-code budget; expired codes always
    fail.
    """
    verification = PackageEmailVerification.objects.filter(package=package).first()
    if verification is None:
        return False, "No verification code has been requested for this request."
    if not matches_recipient(package, email):
        return False, "That email does not match the recipient for this request."
    if verification.is_expired:
        return False, "That verification code has expired. Request a new one."
    if verification.attempts >= MAX_ATTEMPTS:
        return False, "Too many incorrect attempts. Request a new code."

    if verification.code_hash != _hash_code(code.strip(), str(package.uuid)):
        verification.attempts += 1
        verification.save(update_fields=["attempts"])
        remaining = MAX_ATTEMPTS - verification.attempts
        return (
            False,
            f"Incorrect code.{' ' + str(remaining) + ' attempts remaining.' if remaining > 0 else ' Request a new code.'}",
        )

    verification.verified_at = timezone.now()
    verification.attempts = 0
    verification.save(update_fields=["verified_at", "attempts"])
    return True, None
