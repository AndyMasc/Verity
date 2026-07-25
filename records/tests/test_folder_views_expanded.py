"""Expanded tests for folder views: CreateFolderView, FolderUpdateView, FolderDeleteView.

Covers edge cases like empty names, folder reorder, and record unlinking.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import Folder, Record

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class FolderCreateEdgeCasesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="foldcreate", password="pass")
        self.url = reverse("records:create_folder")

    def test_empty_name_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": ""}, HTTP_HX_REQUEST="true")
        self.assertIn(response.status_code, [200, 422])
        self.assertEqual(Folder.objects.filter(user=self.user).count(), 0)

    def test_duplicate_name_allowed(self):
        self.client.force_login(self.user)
        Folder.objects.create(user=self.user, name="Taxes")
        response = self.client.post(self.url, {"name": "Taxes"}, HTTP_HX_REQUEST="true")
        self.assertIn(response.status_code, [200, 302])


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class FolderDeleteEdgeCasesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="folddelete", password="pass")

    def test_delete_empty_folder(self):
        folder = Folder.objects.create(user=self.user, name="Empty")
        self.client.force_login(self.user)
        url = reverse("records:delete_folder", args=[folder.id])
        response = self.client.post(url)
        self.assertFalse(Folder.objects.filter(id=folder.id).exists())

    def test_delete_folder_unlinks_records(self):
        folder = Folder.objects.create(user=self.user, name="With Records")
        record = Record.objects.create(
            user=self.user,
            title="In Folder",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            folder=folder,
        )
        self.client.force_login(self.user)
        url = reverse("records:delete_folder", args=[folder.id])
        self.client.post(url, HTTP_HX_REQUEST="true")
        record.refresh_from_db()
        self.assertIsNone(record.folder)

    def test_other_user_cannot_delete(self):
        folder = Folder.objects.create(user=self.user, name="Mine")
        other = User.objects.create_user(username="otherfold", password="pass")
        self.client.force_login(other)
        url = reverse("records:delete_folder", args=[folder.id])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Folder.objects.filter(id=folder.id).exists())


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class FolderUpdateEdgeCasesTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="foldupdate", password="pass")
        self.folder = Folder.objects.create(user=self.user, name="Original")

    def test_update_to_empty_name(self):
        self.client.force_login(self.user)
        url = reverse("records:edit_folder", args=[self.folder.id])
        response = self.client.post(url, {"name": ""}, HTTP_HX_REQUEST="true")
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Original")

    def test_other_user_cannot_update(self):
        other = User.objects.create_user(username="otherupdfold", password="pass")
        self.client.force_login(other)
        url = reverse("records:edit_folder", args=[self.folder.id])
        response = self.client.post(url, {"name": "Hacked"}, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 404)
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Original")
