"""Shared notification helpers for the reimbursement system."""

from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import escape


def build_package_url(package_uuid: str) -> str:
    site_url = getattr(settings, "SITE_URL", "http://localhost:8000")
    return f"{site_url}/reimbursements/{package_uuid}/"


def _site_context() -> dict:
    from core.services.notifications import build_site_context

    return build_site_context()


def send_package_created_notification(package, recipient) -> None:
    """Notify the recipient that a reimbursement package has been sent to them."""
    from core.services.notifications import send_multi_channel_notification

    package_url = build_package_url(package.uuid)
    creator_name = escape(package.creator.get_full_name() or package.creator.email)
    safe_title = escape(package.title)
    safe_recipient = escape(recipient.get_full_name() or recipient.email)
    amount = package.total_amount

    subject = f'{creator_name} sent you a reimbursement request: "{safe_title}"'

    template_context = {
        "creator_name": creator_name,
        "recipient_name": safe_recipient,
        "title": safe_title,
        "amount": amount,
        "package_url": package_url,
        "records": package.records.filter(is_active=True),
        **_site_context(),
    }

    html_body = render_to_string(
        "reimbursements/email/package_created_message.html", template_context
    )
    text_body = render_to_string(
        "reimbursements/email/package_created_message.txt", template_context
    )

    db_message = f'{creator_name} sent you a reimbursement request for "{safe_title}" (${amount}).'

    send_multi_channel_notification(
        user=recipient,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        webpush_payload={
            "head": "Reimbursement Request",
            "body": db_message,
            "url": package_url,
        },
        send_db=True,
        db_message=db_message,
    )


def send_package_paid_notification(package, payer) -> None:
    """Send email + in-app notification to the package creator when paid."""
    from core.services.notifications import send_multi_channel_notification

    package_url = build_package_url(package.uuid)
    payer_name = escape(payer.get_full_name() or payer.email) if payer else "Someone"
    safe_title = escape(package.title)
    safe_creator = escape(package.creator.get_full_name() or package.creator.email)
    amount = package.total_amount

    subject = f'Your reimbursement "{safe_title}" was paid'

    template_context = {
        "creator_name": safe_creator,
        "payer_name": payer_name,
        "title": safe_title,
        "amount": amount,
        "package_url": package_url,
        **_site_context(),
    }

    html_body = render_to_string("reimbursements/email/package_paid_message.html", template_context)
    text_body = render_to_string("reimbursements/email/package_paid_message.txt", template_context)

    db_message = f'{payer_name} paid ${amount} for "{safe_title}".'

    send_multi_channel_notification(
        user=package.creator,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        webpush_payload={
            "head": "Reimbursement Paid",
            "body": db_message,
            "url": package_url,
        },
        send_db=True,
        db_message=db_message,
    )
