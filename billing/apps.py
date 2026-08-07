from django.apps import AppConfig


class BillingConfig(AppConfig):
    name = "billing"

    def ready(self) -> None:
        import billing.webhooks  # noqa: F401
