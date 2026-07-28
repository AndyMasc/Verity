import os
from locust import HttpUser, task, between


class PapertrailUser(HttpUser):
    wait_time = between(2, 5)

    def on_start(self):
        resp = self.client.get("/accounts/login/")
        csrf = self.client.cookies.get("csrftoken")
        self.client.post(
            "/accounts/login/",
            data={
                "login": os.environ.get("LOCUST_USER", "testuser"),
                "password": os.environ.get("LOCUST_PASS", "testpass"),
                "csrfmiddlewaretoken": csrf,
            },
            headers={"X-CSRFToken": csrf},
        )

    @task(5)
    def view_dashboard(self):
        self.client.get("/dashboard/", name="/dashboard/")

    @task(4)
    def view_records(self):
        self.client.get("/records/view_all_records/", name="/records/")

    @task(3)
    def view_folders(self):
        self.client.get("/records/folders/", name="/records/folders/")

    @task(2)
    def view_documents(self):
        self.client.get("/documents/document_lists/", name="/documents/")

    @task(2)
    def view_notifications(self):
        self.client.get("/notifications/", name="/notifications/")

    @task(1)
    def view_profile(self):
        self.client.get("/profile_page/", name="/profile/")

    @task(1)
    def expense_chart(self):
        self.client.get(
            "/api/expense-chart/?period=6m",
            name="/api/expense-chart/",
        )
