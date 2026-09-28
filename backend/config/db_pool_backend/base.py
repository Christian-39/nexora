"""Pooled MySQL backend with process-wide server-info caching.

Two production problems this solves (both measured, see the audit report):

1. Django's persistent connections (CONN_MAX_AGE) are thread-local. Under
   ASGI every request runs in a single-use ThreadSensitiveContext thread, so
   each request paid a full TCP+TLS+MySQL handshake (~1.3-2.1s to a remote
   database). ``dj_db_conn_pool`` fixes that with a process-wide SQLAlchemy
   QueuePool.

2. Django's MySQL backend caches ``mysql_server_data`` per *DatabaseWrapper
   instance* and computes it by opening a TEMPORARY EXTRA connection and
   running ``SELECT VERSION()``. ASGI creates a fresh wrapper per request
   thread, so every request paid an extra pool checkout + round-trip purely
   to rediscover a server version that cannot change mid-deploy. This
   subclass shares the discovered server data across all wrappers in the
   process (all wrappers point at the same 'default' MySQL server).
"""

from dj_db_conn_pool.backends.mysql.base import (
    DatabaseWrapper as PooledMySQLDatabaseWrapper,
)
from django.utils.functional import cached_property


class DatabaseWrapper(PooledMySQLDatabaseWrapper):
    _shared_server_data = None

    @cached_property
    def mysql_server_data(self):
        cls = DatabaseWrapper
        if cls._shared_server_data is None:
            cls._shared_server_data = PooledMySQLDatabaseWrapper.mysql_server_data.real_func(
                self
            )
        return cls._shared_server_data
