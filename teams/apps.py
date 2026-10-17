from django.apps import AppConfig


class TeamsConfig(AppConfig):
    name = "teams"

    def ready(self) -> None:
        import teams.orgs  # noqa: F401
