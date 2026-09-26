import sys
from unittest.mock import MagicMock, patch

import pytest
from django.apps import apps as django_apps

from core import apps as core_apps


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["manage.py", "test"], True),
        (["manage.py", "shell"], True),
        (["manage.py", "runserver"], False),
        (["gunicorn", "Verity.wsgi"], False),
        (["manage.py"], False),
    ],
)
def test_is_local_only_process_checks_management_command(monkeypatch, argv, expected):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(sys, "argv", argv)

    assert core_apps.is_local_only_process() is expected


def test_is_local_only_process_detects_pytest(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["gunicorn", "Verity.wsgi"])

    assert core_apps.is_local_only_process() is True


@pytest.mark.parametrize(("local_only", "autocapture"), [(True, False), (False, True)])
def test_ready_disables_exception_autocapture_for_local_only_process(
    monkeypatch, settings, local_only, autocapture
):
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    monkeypatch.setattr(core_apps, "posthog_client", None)
    monkeypatch.setattr(core_apps, "is_local_only_process", lambda: local_only)
    posthog_cls = MagicMock()
    monkeypatch.setattr(core_apps, "Posthog", posthog_cls)

    with (
        patch("core.apps.atexit.register"),
        patch("core.posthog_logs.configure_posthog_log_export"),
    ):
        django_apps.get_app_config("core").ready()

    assert posthog_cls.call_args.kwargs["enable_exception_autocapture"] is autocapture
