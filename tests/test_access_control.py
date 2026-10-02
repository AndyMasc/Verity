"""Anonymous access control for every authenticated view.

This was twenty-one copies of the same two lines, one per view test class, with
the assertion drifting between a strict 302 and a loose 302/300. A new protected
view now gets one row here instead of a new copy of the pattern, and the whole
set is visible in one place.

Views that scope to an object are listed against a record or document created in
setUp; the row only has to say which one.
"""

import pytest
from django.test import Client
from django.urls import reverse

from conftest import RecordFactory, UserFactory
from documents.models import DocumentData

# (verb, url name, object the route needs)
PROTECTED_VIEWS = [
    ("get", "core:dashboard", None),
    ("get", "core:profile_page", None),
    ("get", "documents:upload_document", None),
    ("get", "documents:document_list_view", None),
    ("get", "records:add_record_manual", None),
    ("get", "records:view_folders", None),
    ("get", "records:create_folder", None),
    ("get", "records:manual_merge", None),
    ("get", "records:view_all_records", None),
    ("get", "records:bulk_unarchive", None),
    ("get", "records:bulk_archive", None),
    ("get", "documents:add_support_docs", "record"),
    ("get", "records:record_history", "record"),
    ("get", "records:record_detail", "record"),
    ("get", "documents:view_document", "document"),
    ("get", "records:check_ocr_status", "document"),
    ("post", "documents:confirm_upload", None),  # document id travels in the body
    ("post", "documents:delete_document", "document"),
    ("post", "records:delete_record", "record"),
    ("post", "records:archive_record", "record"),
]


@pytest.mark.parametrize("verb,url_name,needs", PROTECTED_VIEWS)
def test_anonymous_is_redirected_to_login(db, verb, url_name, needs):  # noqa: ARG001
    """An anonymous visitor must be redirected to login, never served the view."""
    user = UserFactory()
    args = []
    if needs == "record":
        args = [RecordFactory(user=user).pk]
    elif needs == "document":
        args = [
            DocumentData.objects.create(
                user=user, title="Receipt", filepath="users/1/receipt.pdf"
            ).pk
        ]

    response = getattr(Client(), verb)(reverse(url_name, args=args))
    assert response.status_code == 302, (
        f"{url_name} served an anonymous {verb.upper()} "
        f"({response.status_code} instead of a login redirect)"
    )
