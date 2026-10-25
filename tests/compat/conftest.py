"""Compatibility harness: run the same script on the real database and on s3rds, compare every step.

A script is a list of (sql, params). Each step is executed on both sides and the two outcomes are
compared: result rows (values AND Python types), rowcount for writes, lastrowid for inserts, and the
error (class name + code) when a step fails. The first difference fails the case with a readable diff.

Cases that are known to differ today are marked xfail with the task that will fix them; when a fix
lands they turn XPASS (strict) and the marker must be removed -- that is the compatibility report.
"""
import base64
import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import pytest

import lambda_function as lf
import s3rds
from s3rds import pymysql as s3mysql
from s3rds import sqlite as s3sqlite

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()
import compat_report  # report plugin lives in tests/ so it sees the whole run


@dataclass
class Outcome:
    rows: object      # list[dict] for SELECT-like statements, else None
    rowcount: object
    lastrowid: object
    error: object     # (class name, code) or None

    def typed_rows(self):
        return None if self.rows is None else [
            {k: (type(v).__name__, v) for k, v in r.items()} for r in self.rows]


def run_step(conn, dict_cursor, sql, params):
    try:
        with conn.cursor(dict_cursor) as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()] if cur.description else None
            return Outcome(rows, cur.rowcount, cur.lastrowid, None)
    except Exception as e:  # noqa: BLE001 - we compare whatever each side raises
        code = e.args[0] if e.args and isinstance(e.args[0], int) else None
        return Outcome(None, None, None, (type(e).__name__, code))


def compare(step, real, ours):
    """step = (sql, params) or (sql, params, {"ignore": "rowcount,lastrowid"}) for documented MySQL quirks."""
    sql = step[0]
    ignore = set((step[2].get("ignore") if len(step) > 2 else "").split(","))
    if real.error or ours.error:
        assert real.error == ours.error, f"{sql}\n  real:  {real.error}\n  s3rds: {ours.error}"
        return
    if real.rows is not None:
        assert real.typed_rows() == ours.typed_rows(), \
            f"{sql}\n  real:  {real.typed_rows()}\n  s3rds: {ours.typed_rows()}"
    else:
        if "rowcount" not in ignore:
            assert real.rowcount == ours.rowcount, f"{sql}\n  rowcount real={real.rowcount} s3rds={ours.rowcount}"
        if sql.lstrip().upper().startswith("INSERT") and "lastrowid" not in ignore:  # defined after INSERT only
            assert real.lastrowid == ours.lastrowid, f"{sql}\n  lastrowid real={real.lastrowid} s3rds={ours.lastrowid}"


def in_process(module, fake_s3, monkeypatch, bucket):
    """An s3rds connection whose HTTP layer calls the lambda handler in-process."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    c = module.connect(host="in-process.test", user="admin", password="pw", database="compat")

    def post(path, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                 "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
        resp = lf.lambda_handler(event, None)
        data = json.loads(resp["body"])
        if resp["statusCode"] >= 400:
            raise s3rds.sqlite.Error(resp["statusCode"], data.get("error", ""))
        return data

    monkeypatch.setattr(c, "_post", post)
    monkeypatch.setattr(c, "list_databases", lambda: ["compat"])
    c.create_database()
    return c


# ---- MySQL side ----

@pytest.fixture
def real_mysql():
    dsn = os.environ.get("MYSQL_DSN")
    if not dsn:
        pytest.skip("set MYSQL_DSN (see tests/compat/docker-compose.yml) to run against a real MySQL")
    pymysql = pytest.importorskip("pymysql")
    u = urlparse(dsn)
    conn = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password,
                           database=u.path.lstrip("/"), autocommit=True)
    if not compat_report.versions["mysql"]:
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            compat_report.versions["mysql"] = f"MySQL {cur.fetchone()[0]} (Docker)"
    yield conn, pymysql.cursors.DictCursor
    conn.close()


@pytest.fixture
def s3_mysql(fake_s3, monkeypatch, bucket):
    c = in_process(s3mysql, fake_s3, monkeypatch, bucket)
    return c, s3mysql.cursors.DictCursor


@pytest.fixture
def mysql_pair(real_mysql, s3_mysql):
    """(run_on_both) -> executes a script on both sides and compares step by step."""
    (real, real_cur), (ours, ours_cur) = real_mysql, s3_mysql

    def run(script):
        for step in script:
            sql, params = step[0], step[1]
            compare(step, run_step(real, real_cur, sql, params), run_step(ours, ours_cur, sql, params))
    run.both = lambda sql, params=(): (run_step(real, real_cur, sql, params), run_step(ours, ours_cur, sql, params))
    return run


# ---- SQLite side (no Docker: the reference is the standard library) ----

@pytest.fixture
def sqlite_pair(fake_s3, monkeypatch, bucket, tmp_path):
    import sqlite3

    class RefCursor:
        """sqlite3 wrapped to look like a pymysql DictCursor for run_step()."""
        def __init__(self, conn):
            self._conn = conn
            self._cur = None

        def execute(self, sql, params):
            self._cur = self._conn.execute(sql, params or [])
            self._conn.commit()

        @property
        def description(self):
            return self._cur.description

        def fetchall(self):
            cols = [d[0] for d in self._cur.description]
            return [dict(zip(cols, r)) for r in self._cur.fetchall()]

        @property
        def rowcount(self):
            return self._cur.rowcount

        @property
        def lastrowid(self):
            return self._cur.lastrowid

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class RefConn:
        def __init__(self, path):
            self._c = sqlite3.connect(path, isolation_level=None)
            self._c.execute("PRAGMA foreign_keys=ON")

        def cursor(self, _cls=None):
            return RefCursor(self._c)

    real = RefConn(str(tmp_path / "ref.sqlite"))
    ours = in_process(s3sqlite, fake_s3, monkeypatch, bucket)

    def run(script):
        for step in script:
            sql, params = step[0], step[1]
            r, o = run_step(real, None, sql, params), run_step(ours, s3sqlite.cursors.DictCursor, sql, params)
            # error classes differ by construction (sqlite3.X vs s3rds Error); compare that both failed or neither
            if r.error or o.error:
                assert bool(r.error) == bool(o.error), f"{sql}\n  real: {r.error}\n  s3rds: {o.error}"
                continue
            compare(step, r, o)
    return run
