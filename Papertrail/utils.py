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
_PAGINATOR_VERSION_TTL = 60 * 60 * 24 * 7  # 7 days

_UNKNOWN_NAMESPACE = "unknown"


def bump_paginator_count_version(namespace: str, user_id: int) -> None:
    """Bump the per-user paginator count version for "namespace".

    Called whenever rows backing a cached paginated list change so that
    stale cached COUNT results are not served until the TTL expires. The
    version is stored per "(namespace, user_id)" so only the affected
    user's cached counts are invalidated.
    """
    key = f"pg_version:{namespace}:{user_id}"
    if not cache.add(key, 1, timeout=_PAGINATOR_VERSION_TTL):
        try:
            cache.incr(key)
        except ValueError:
            cache.add(key, 1, timeout=_PAGINATOR_VERSION_TTL)


class CachedPaginator(Paginator):
    """A Paginator that caches expensive queryset COUNT queries.

    Django's default paginator runs a "SELECT COUNT(*)" on every
    page load, which can be slow for large tables. This subclass
    caches the count result keyed by the query's WHERE clause and
    model table, avoiding redundant queries within the TTL window.

    The cache key is versioned per "(namespace, user_id)". Background
    tasks that create or mutate rows call :func:`bump_paginator_count_version`
    so newly inserted rows are reflected immediately instead of being
    hidden by a stale cached count.
    """

    def __init__(
        self,
        object_list,
        per_page,
        *,
        namespace: str | None = None,
        user_id: int | None = None,
        orphans: int = 0,
        allow_empty_first_page: bool = True,
    ):
        super().__init__(
            object_list,
            per_page,
            orphans=orphans,
            allow_empty_first_page=allow_empty_first_page,
        )
        self._namespace = namespace or self._default_namespace()
        self._user_id = user_id

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

    def _default_namespace(self) -> str:
        if isinstance(self.object_list, QuerySet):
            return self.object_list.model._meta.model_name
        return _UNKNOWN_NAMESPACE

    @property
    def _version_key(self) -> str:
        return f"pg_version:{self._namespace}:{self._user_id or 0}"

    def _make_count_cache_key(self) -> str:
        assert isinstance(self.object_list, QuerySet)
        where = str(self.object_list.query.where)
        version = cache.get(self._version_key) or 0
        raw = f"pg:{self.object_list.query.model._meta.db_table}:{where}:{version}"
        return f"pg:{hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()}"
