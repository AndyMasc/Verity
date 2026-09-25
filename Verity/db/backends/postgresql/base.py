"""PostgreSQL backend that tolerates Neon's transient connection drops.

Neon suspends idle computes ("scale to zero") and its pooled endpoint
(PgBouncer) recycles connections, so a connection attempt can be dropped
mid-handshake with "server closed the connection unexpectedly" or "SSL
connection has been closed unexpectedly". The compute wakes in a few hundred
milliseconds and the next attempt succeeds. This backend retries exactly those
transient messages with short backoff; all other errors (bad password, TLS
failure, ...) still raise immediately.
"""

import time

from django.db.backends.postgresql import base as postgresql_base

_TRANSIENT_MARKERS = (
    "server closed the connection unexpectedly",
    "SSL connection has been closed unexpectedly",
    "connection reset by peer",
    "connection terminated by the server",
    "ending connection because it has been idle",
)

_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 0.5


class DatabaseWrapper(postgresql_base.DatabaseWrapper):
    """Django PostgreSQL backend that retries transient connect failures."""

    def get_new_connection(self, conn_params):
        backoff = _BASE_BACKOFF
        for attempt in range(_MAX_ATTEMPTS):
            try:
                return super().get_new_connection(conn_params)
            except postgresql_base.Database.OperationalError as exc:
                if not any(marker in str(exc) for marker in _TRANSIENT_MARKERS):
                    raise
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2
