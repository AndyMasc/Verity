"""Shared utility classes for the Papertrail project."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import QuerySet
from django.utils.functional import cached_property

if TYPE_CHECKING:
    pass

PAGINATOR_COUNT_CACHE_TTL = 60


class CachedPaginator(Paginator):
    """A Paginator that caches expensive queryset COUNT queries.

    Django's default paginator runs a ``SELECT COUNT(*)`` on every
    page load, which can be slow for large tables. This subclass
    caches the count result keyed by the query's WHERE clause and
    model table, avoiding redundant queries within the TTL window.
    """

    @cached_property
    def count(self) -> int:
        if not isinstance(self.object_list, QuerySet):
            return Paginator.count.__get__(self, type(self))
        cache_key = self._make_count_cache_key()
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        result = Paginator.count.__get__(self, type(self))
        cache.set(cache_key, result, PAGINATOR_COUNT_CACHE_TTL)
        return result

    def _make_count_cache_key(self) -> str:
        assert isinstance(self.object_list, QuerySet)
        where = str(self.object_list.query.where)
        raw = f"pg:{self.object_list.query.model._meta.db_table}:{where}"  # type: ignore[union-attr]
        return f"pg:{hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()}"
