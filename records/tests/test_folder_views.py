from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from django.urls import reverse

from records.models import Record, Folder


class FolderListViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="folderlist", password="pass")
        self.url = reverse("records:view_folders")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/folders.html")

    def test_context_has_folders(self):
        self.client.force_login(self.user)
        Folder.objects.create(user=self.user, name="My Folder")
        response = self.client.get(self.url)
        self.assertIn("folders", response.context)

    def test_only_user_folders_shown(self):
        user2 = User.objects.create_user(username="otherfl", password="pass")
        Folder.objects.create(user=user2, name="Other's")
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["folders"]), 0)


class CreateFolderViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="createf", password="pass")
        self.url = reverse("records:create_folder")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_form(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/partials/folders/create_folder_modal.html")

    def test_post_valid(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": "New Folder"}, HTTP_HX_REQUEST="true")
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Folder.objects.filter(name="New Folder", user=self.user).exists())


class FolderUpdateViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="updf", password="pass")
        self.folder = Folder.objects.create(user=self.user, name="Old Name")
        self.url = reverse("records:edit_folder", args=[self.folder.id])

    def test_owner_can_update(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"name": "New Name"}, HTTP_HX_REQUEST="true")
        self.assertIn(response.status_code, [200, 302])
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "New Name")

    def test_other_user_cannot_update(self):
        user2 = User.objects.create_user(username="otherupf", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url, {"name": "Hacked"})
        self.assertEqual(response.status_code, 404)
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Old Name")

    def test_get_method(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


class FolderDeleteViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="delf", password="pass")
        self.folder = Folder.objects.create(user=self.user, name="To Delete")
        self.url = reverse("records:delete_folder", args=[self.folder.id])

    def test_owner_can_delete(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.assertFalse(Folder.objects.filter(id=self.folder.id).exists())

    def test_other_user_cannot_delete(self):
        user2 = User.objects.create_user(username="otherdelf", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Folder.objects.filter(id=self.folder.id).exists())

    def test_delete_removes_folder_from_records(self):
        self.client.force_login(self.user)
        record = Record.objects.create(
            user=self.user,
            title="Folder Record",
            record_type="expense_receipt",
            folder=self.folder,
        )
        record_id = record.id
        response = self.client.post(self.url, HTTP_HX_REQUEST="true")
        self.assertFalse(Folder.objects.filter(id=self.folder.id).exists())
        remaining = Record.objects.filter(id=record_id).exists()
        if remaining:
            record.refresh_from_db()
            self.assertIsNone(record.folder)
