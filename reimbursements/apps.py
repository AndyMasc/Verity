from django.apps import AppConfig


class ReimbursementsConfig(AppConfig):
    name = "reimbursements"

    def ready(self):
        import reimbursements.signals
        import reimbursements.webhooks  # noqa: F401
