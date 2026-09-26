"""
NEXORA — project package.

MySQL driver selection happens here, before Django loads the database backend.

``mysqlclient`` (a C extension) is preferred because it is the fastest option,
but it can only be built where the MySQL client headers exist. On a managed
Python runtime that does not ship them the pure-Python ``PyMySQL`` driver is
registered under the same module name instead, so the very same
``django.db.backends.mysql`` settings keep working. Neither driver changes any
query, transaction or schema behaviour.
"""

try:  # pragma: no cover - depends on the host; both branches are valid
    import MySQLdb  # noqa: F401
except ImportError:  # pragma: no cover
    try:
        import pymysql

        pymysql.install_as_MySQLdb()
    except ImportError:
        # Neither driver is installed: only meaningful for SQLite development
        # and the test suite, which never touch MySQL.
        pass
