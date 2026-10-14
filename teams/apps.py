from django.apps import AppConfig


class TeamsConfig(AppConfig):
    name = "teams"

    def ready(self) -> None:
        from . import signals  # noqa: F401
