"""Shared pytest fixtures and factory definitions for Papertrail tests."""

from __future__ import annotations

from datetime import date

import dramatiq
import factory
import pytest

from billing.models import CustomUser as User


class UserFactory(factory.django.DjangoModelFactory):
    """Factory for creating test users."""

    class Meta:
        model = User
        django_get_or_create = ("username",)

    username = factory.Sequence(lambda n: f"user{n}")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    password = factory.PostGenerationMethodCall("set_password", "testpass123")


class FolderFactory(factory.django.DjangoModelFactory):
    """Factory for creating test folders."""

    class Meta:
        model = "records.Folder"

    user = factory.SubFactory(UserFactory)
    name = factory.Sequence(lambda n: f"Folder {n}")


class RecordFactory(factory.django.DjangoModelFactory):
    """Factory for creating test records."""

    class Meta:
        model = "records.Record"

    user = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Record {n}")
    record_type = "expense_receipt"
    transaction_date = factory.LazyFunction(lambda: date.today())
    is_active = True


class PlaidItemFactory(factory.django.DjangoModelFactory):
    """Factory for creating test Plaid items."""

    class Meta:
        model = "plaid_integration.PlaidItem"

    user = factory.SubFactory(UserFactory)
    item_id = factory.Sequence(lambda n: f"plaid-item-{n}")
    access_token = factory.Sequence(lambda n: f"access-token-{n}")


class DocumentDataFactory(factory.django.DjangoModelFactory):
    """Factory for creating test documents."""

    class Meta:
        model = "documents.DocumentData"

    user = factory.SubFactory(UserFactory)
    title = factory.Sequence(lambda n: f"Document {n}")
    filepath = factory.Sequence(lambda n: f"users/1/doc-{n}.pdf")
    status = "pending_upload"


@pytest.fixture
def user(db) -> User:  # type: ignore[no-untyped-def]  # noqa: ARG001
    """Create and return a test user."""
    return UserFactory()  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def _locmem_cache(settings):  # type: ignore[no-untyped-def]
    """Isolate tests from the shared Redis cache used by the dev processes.

    The default cache backend is Redis (shared with the running web server and
    background workers). Tests override it with a per-process LocMemCache so
    ``cache.clear()`` and cache key churn never touch the live cache.
    """
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@pytest.fixture
def other_user(db) -> User:  # type: ignore[no-untyped-def]  # noqa: ARG001
    """Create a second test user for isolation tests."""
    return UserFactory()  # type: ignore[return-value]


@pytest.fixture
def broker():
    broker = dramatiq.get_broker()
    broker.flush_all()
    return broker


@pytest.fixture
def worker(broker):
    worker = dramatiq.Worker(broker, worker_timeout=100)
    worker.start()
    yield worker
    worker.stop()
