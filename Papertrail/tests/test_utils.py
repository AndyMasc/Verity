"""Tests for Papertrail shared utilities.

Covers CachedPaginator count caching, _make_count_cache_key,
and non-QuerySet fallback.
"""

from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from Papertrail.utils import PAGINATOR_COUNT_CACHE_TTL, CachedPaginator

User = get_user_model()


class CachedPaginatorTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="paginator_user", password="pass")

    def test_count_caches_result(self):
        from records.models import Record

        qs = Record.objects.filter(user=self.user)
        paginator = CachedPaginator(qs, 10)
        with (
            patch("django.core.cache.cache.get", return_value=None) as mock_get,
            patch("django.core.cache.cache.set") as mock_set,
        ):
            count = paginator.count
            mock_set.assert_called_once()

    def test_count_returns_cached_value(self):
        from records.models import Record

        qs = Record.objects.filter(user=self.user)
        paginator = CachedPaginator(qs, 10)
        with patch("django.core.cache.cache.get", return_value=42):
            count = paginator.count
            assert count == 42

    def test_non_queryset_falls_back_to_parent(self):
        data = list(range(25))
        paginator = CachedPaginator(data, 10)
        assert paginator.count == 25

    def test_make_count_cache_key_format(self):
        from records.models import Record

        qs = Record.objects.filter(user=self.user)
        paginator = CachedPaginator(qs, 10)
        key = paginator._make_count_cache_key()
        assert key.startswith("pg:")
        assert len(key) > 3
