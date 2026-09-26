import sys
from unittest.mock import patch

import pytest
from django.apps import apps

from core import apps as core_apps


@pytest.fixture
def posthog_env(monkeypatch):
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "phc_test")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    monkeypatch.setattr(core_apps, "posthog_client", None)


def test_ready_skips_posthog_under_pytest(posthog_env):
    with patch.object(core_apps, "Posthog") as posthog_cls:
        apps.get_app_config("core").ready()

    posthog_cls.assert_not_called()
    assert core_apps.posthog_client is None


@pytest.mark.parametrize("command", ["shell", "shell_plus", "test"])
def test_skip_posthog_for_shell_and_test_commands(monkeypatch, command):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(sys, "argv", ["manage.py", command])

    assert core_apps._skip_posthog() is True


@pytest.mark.parametrize(
    "argv",
    [["manage.py", "runserver"], ["gunicorn", "Verity.wsgi"], ["manage.py", "rundramatiq"]],
)
def test_skip_posthog_false_for_serving_processes(monkeypatch, argv):
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.setattr(sys, "argv", argv)

    assert core_apps._skip_posthog() is False
