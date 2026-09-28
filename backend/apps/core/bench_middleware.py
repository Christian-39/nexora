"""Per-request SQL/connection instrumentation (benchmark only)."""
import os
import threading
import time

from django.db import connection
from django.db.backends.base.base import BaseDatabaseWrapper

_LOG = os.environ.get("BENCH_LOG", "/tmp/bench.log")
_lock = threading.Lock()

# --- count real (re)connects process-wide ---------------------------------
_orig_connect = BaseDatabaseWrapper.connect
_conn_counter = {"n": 0}


def _counting_connect(self):
    t0 = time.perf_counter()
    result = _orig_connect(self)
    dt = (time.perf_counter() - t0) * 1000
    with _lock:
        _conn_counter["n"] += 1
        with open(_LOG, "a") as fh:
            fh.write(
                f"CONNECT pid={os.getpid()} tid={threading.get_ident()} "
                f"n={_conn_counter['n']} ms={dt:.1f}\n"
            )
            if os.environ.get("BENCH_TRACE_CONNECT"):
                import traceback

                fh.write("".join(traceback.format_stack()[-14:]) + "\n")
    return result


BaseDatabaseWrapper.connect = _counting_connect


class BenchMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        queries = []

        def recorder(execute, sql, params, many, context):
            t0 = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                queries.append(((time.perf_counter() - t0) * 1000, sql[:160]))

        conn_before = _conn_counter["n"]
        t0 = time.perf_counter()
        with connection.execute_wrapper(recorder):
            response = self.get_response(request)
        total = (time.perf_counter() - t0) * 1000
        sql_ms = sum(q[0] for q in queries)
        slowest = max(queries, key=lambda q: q[0]) if queries else (0, "-")
        new_conns = _conn_counter["n"] - conn_before
        with _lock:
            with open(_LOG, "a") as fh:
                fh.write(
                    f"REQ pid={os.getpid()} tid={threading.get_ident()} "
                    f"{request.method} {request.path} {response.status_code} "
                    f"total={total:.1f}ms nq={len(queries)} sql={sql_ms:.1f}ms "
                    f"newconn={new_conns} slowest={slowest[0]:.1f}ms :: {slowest[1]}\n"
                )
        return response
