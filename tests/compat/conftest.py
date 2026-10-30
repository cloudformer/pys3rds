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
from s3_sqlite import sqlite3 as ENGINE   # the server's SQLite module: sqlean (pinned) on Lambda
import s3rds
from s3rds import psycopg2 as s3pg
from s3rds import pymysql as s3mysql
from s3rds import sqlite as s3sqlite

PIN = open(os.path.join(os.path.dirname(__file__), "..", "..", "src", "db", "requirements.txt")).read().split()[-1]


@pytest.fixture(autouse=True)
def pinned_engine():
    """Every comparison runs on the deployed engine, never on whatever sqlite3 this machine has."""
    if ENGINE.__name__ != "sqlean":
        pytest.skip(f"the compatibility suite needs the server's pinned engine: pip install {PIN} (Python >= 3.10)")


AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()
import compat_aws     # --aws: the deployed stack instead of the in-process handler
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
            cur.execute(sql, params or None)  # pymysql %-formats the SQL whenever args is not None
            rows = [dict(r) for r in cur.fetchall()] if cur.description else None
            return Outcome(rows, cur.rowcount, cur.lastrowid, None)
    except Exception as e:  # noqa: BLE001 - we compare whatever each side raises
        code = getattr(e, "pgcode", None) or (e.args[0] if e.args and isinstance(e.args[0], int) else None)
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


def in_process(module, fake_s3, monkeypatch, bucket, **connect_kw):
    """An s3rds connection whose HTTP layer calls the lambda handler in-process."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    c = module.connect(host="in-process.test", user="admin", password="pw", database="compat", **connect_kw)

    def post(path, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                 "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
        resp = lf.lambda_handler(event, None)
        data = json.loads(resp["body"])
        if resp["statusCode"] >= 400:  # the same reply -> exception mapping as over HTTP
            raise s3rds.sqlite._error(resp["statusCode"], data.get("error", ""))
        return data

    monkeypatch.setattr(c, "_post", post)
    monkeypatch.setattr(c, "list_databases", lambda: ["compat"])
    c.create_database()
    return c


# ---- MySQL side ----

@pytest.fixture
def real_mysql():
    dsn = os.environ.get("MYSQL_DSN")
    if not dsn and os.environ.get("COMPAT_DRY"):
        yield None, None   # dry run: execute the s3rds side only and print what it does (no Docker needed)
        return
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


@pytest.fixture(scope="session")
def deployed(request):
    """--aws: the deployed stack and its own test database `compat`, emptied once per run as the Docker
    container starts empty. None without --aws."""
    live = compat_aws.endpoint(request.config, url="db_url", user="console_auth_username", password="console_auth_password")
    if live:
        c = s3mysql.connect(host=live["url"], user=live["user"], password=live["password"], database="compat")
        c.create_database()
        for _ in range(5):   # tables referenced by a foreign key go in a later pass
            names = [r[0] for r in c._raw("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND name NOT LIKE 'sqlite_%'")["rows"]]
            for name in names:
                try:
                    c._raw(f'DROP TABLE "{name}"')
                except s3mysql.Error:
                    pass
            if not names:
                break
        c.close()
    return live


@pytest.fixture
def s3_mysql(deployed, fake_s3, monkeypatch, bucket):
    if deployed:
        c = s3mysql.connect(host=deployed["url"], user=deployed["user"], password=deployed["password"],
                            database="compat")
    else:
        c = in_process(s3mysql, fake_s3, monkeypatch, bucket)
    yield c, s3mysql.cursors.DictCursor
    c.close()


@pytest.fixture
def mysql_pair(real_mysql, s3_mysql):
    """(run_on_both) -> executes a script on both sides and compares step by step."""
    return _pair(real_mysql, s3_mysql, "MySQL")


def _pair(real_side, our_side, name):
    (real, real_cur), (ours, ours_cur) = real_side, our_side
    dry = real is None

    def both(sql, params=()):
        o = run_step(ours, ours_cur, sql, params)
        if dry:
            print(f"  {'ERR ' + str(o.error) if o.error else 'ok  '} {sql[:90]}")
            return o, o
        return run_step(real, real_cur, sql, params), o

    def run(script):
        for step in script:
            r, o = both(step[0], step[1])
            if not dry:
                compare(step, r, o)
    run.both = both

    def unsupported(sql, params=()):
        """MySQL runs it; s3rds must refuse it with NotSupportedError (never silently do something else)."""
        r, o = both(sql, params)
        assert dry or r.error is None, f"reference {name} rejected the statement itself: {r.error}"
        assert o.error and o.error[0] in ("NotSupportedError", "FeatureNotSupported"), \
            f"{sql}\n  s3rds did not refuse: {o.error or o.rows}"   # psycopg2 raises 0A000 as FeatureNotSupported
    run.unsupported = unsupported

    def many(sql, seq):
        """executemany on both sides; rowcount must be the total."""
        counts = []
        for conn, cur_cls in ([(ours, ours_cur)] if dry else [(real, real_cur), (ours, ours_cur)]):
            with conn.cursor(cur_cls) as cur:
                cur.executemany(sql, seq)
                counts.append(cur.rowcount)
        assert len(set(counts)) == 1, f"executemany rowcount real={counts[0]} s3rds={counts[-1]}"
    run.many = many
    return run


# ---- PostgreSQL side (the same harness; errors compare class name + SQLSTATE) ----

@pytest.fixture
def real_postgres():
    dsn = os.environ.get("PG_DSN")
    if not dsn and os.environ.get("COMPAT_DRY"):
        yield None, None
        return
    if not dsn:
        pytest.skip("set PG_DSN (see tests/compat/docker-compose.yml) to run against a real PostgreSQL")
    psycopg2 = pytest.importorskip("psycopg2")
    import psycopg2.extras
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    if not compat_report.versions["postgres"]:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            compat_report.versions["postgres"] = f"PostgreSQL {cur.fetchone()[0]} (Docker)"
    yield conn, psycopg2.extras.RealDictCursor
    conn.close()


@pytest.fixture
def s3_postgres(deployed, fake_s3, monkeypatch, bucket):
    if deployed:
        c = s3pg.connect(host=deployed["url"], user=deployed["user"], password=deployed["password"], dbname="compat")
    else:
        c = in_process(s3pg, fake_s3, monkeypatch, bucket)
    c.autocommit = True
    yield c, s3pg.extras.RealDictCursor
    c.close()


class _ByFactory:
    """psycopg2's cursor() takes the cursor class as cursor_factory= (its first argument is a name)."""
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, cls=None):
        return self._conn.cursor(cursor_factory=cls)


@pytest.fixture
def pg_pair(real_postgres, s3_postgres):
    """The same as mysql_pair: pg_pair(script), .both(sql), .unsupported(sql), .many(sql, seq)."""
    (real, real_cur), (ours, ours_cur) = real_postgres, s3_postgres
    return _pair((real and _ByFactory(real), real_cur), (_ByFactory(ours), ours_cur), "PostgreSQL")


# ---- SQLite side (no Docker: the reference is the server's pinned SQLite module, called directly) ----

@pytest.fixture
def sqlite_pair(fake_s3, monkeypatch, bucket, tmp_path):
    sqlite3 = ENGINE

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
            compare(step, r, o)  # errors: the same DB-API class (sqlite3.IntegrityError <-> s3rds IntegrityError)
    return run


# ---- SQLAlchemy side (no Docker: the reference is SQLAlchemy on the server's pinned SQLite module) ----

@pytest.fixture
def sqlalchemy_pair(fake_s3, monkeypatch, bucket, tmp_path):
    """run(scenario) -> scenario(engine, other) on SQLAlchemy + sqlite3 and on sqlite+s3rds; the returned
    values must be equal. `other` is a second engine on the same database (another client)."""
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.dialects import registry
    registry.register("sqlite.s3rds", "s3rds.sqlite", "SQLAlchemyDialect")  # pip install registers it

    def ref_engine():
        e = sa.create_engine(f"sqlite:///{tmp_path / 'ref.sqlite'}", module=ENGINE)
        # the s3rds server enforces foreign keys on every connection; give the reference the same
        sa.event.listen(e, "connect", lambda c, _r: c.execute("PRAGMA foreign_keys=ON"))
        return e

    def s3_engine():
        return sa.create_engine("sqlite+s3rds://admin:pw@in-process.test/compat", creator=lambda: in_process(
            s3sqlite, fake_s3, monkeypatch, bucket, autocommit=False))

    if not compat_report.versions.get("sqlalchemy"):
        compat_report.versions["sqlalchemy"] = f"SQLAlchemy {sa.__version__}"

    def run(scenario):
        real = scenario(ref_engine(), ref_engine())
        ours = scenario(s3_engine(), s3_engine())
        assert real == ours, f"\n  real:  {real}\n  s3rds: {ours}"
    return run


# ---- Django side (no Docker: the reference is Django's own SQLite backend on the server's pinned SQLite module) ----
# Django is configured once per process: aliases ref / ref2 (the reference, one file per test) and s3 / s3b
# (ENGINE "s3rds.sqlite", served in-process); the second alias of each pair plays another client.

_DJANGO_TYPES = {}  # what django/db/backends/sqlite3/base.py registers on sqlite3: {"converters": …, "adapters": …}


def _django_reference_backend():
    """django.db.backends.sqlite3 with the pinned engine in place of the standard library's sqlite3 module
    (registered as the module compat_django_reference.base, which ENGINE names)."""
    import datetime
    import decimal
    import sys
    import types
    from collections.abc import Mapping

    from django.db.backends.sqlite3 import base as dj
    from django.db.backends.sqlite3 import features as dj_features
    from django.db.backends.sqlite3._functions import register as register_functions
    from django.utils.dateparse import parse_date, parse_datetime, parse_time

    # Django registers these module-wide; the in-process server registers its own on the same module (typed
    # MySQL results), so the reference gets Django's only while it runs (reference_types())
    _DJANGO_TYPES["converters"] = {"BOOL": b"1".__eq__, **{
        name: (lambda parse: lambda s: parse(s.decode()))(parse)
        for name, parse in (("DATE", parse_date), ("TIME", parse_time), ("DATETIME", parse_datetime),
                            ("TIMESTAMP", parse_datetime))}}
    _DJANGO_TYPES["adapters"] = {(decimal.Decimal, ENGINE.PrepareProtocol): str,
                                 (datetime.date, ENGINE.PrepareProtocol): lambda v: v.isoformat(),
                                 (datetime.datetime, ENGINE.PrepareProtocol): lambda v: v.isoformat(" ")}

    def convert(query, params):
        names = list(params) if isinstance(params, Mapping) else None
        return dj.SQLiteCursorWrapper.convert_query(None, query, param_names=names)

    class Cursor(ENGINE.Cursor):
        def execute(self, query, params=None):
            return super().execute(query) if params is None else super().execute(convert(query, params), params)

        def executemany(self, query, param_list):
            param_list = list(param_list)
            return super().executemany(convert(query, param_list[0] if param_list else ()), param_list)

    class Features(dj_features.DatabaseFeatures):
        supports_aggregate_order_by_clause = ENGINE.sqlite_version_info >= (3, 44, 0)
        max_query_params = 32766  # sqlean's Connection has no getlimit(); SQLite's default limit

    class DatabaseWrapper(dj.DatabaseWrapper):
        Database = ENGINE
        features_class = Features

        def get_new_connection(self, conn_params):
            conn = ENGINE.connect(**conn_params)
            register_functions(conn)
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA legacy_alter_table = OFF")
            return conn

        def create_cursor(self, name=None):
            return self.connection.cursor(factory=Cursor)

    module = types.ModuleType("compat_django_reference.base")
    module.DatabaseWrapper = DatabaseWrapper
    sys.modules["compat_django_reference"] = types.ModuleType("compat_django_reference")
    sys.modules["compat_django_reference.base"] = module


def _django_test_app():
    """The installed app (label "compat") test_django.py's models belong to: Django builds reverse relations
    (cascade delete, author.books, Count("books")) only for models of installed apps."""
    import sys
    import types

    from django.apps import AppConfig

    class CompatConfig(AppConfig):
        name = "compat_django_app"
        label = "compat"
        path = os.path.dirname(os.path.abspath(__file__))

    package = types.ModuleType("compat_django_app")
    package.__path__ = []  # no models submodule: the models are defined in test_django.py
    apps_module = types.ModuleType("compat_django_app.apps")
    apps_module.CompatConfig = CompatConfig
    sys.modules.update({"compat_django_app": package, "compat_django_app.apps": apps_module})


def _django_configure():
    import django
    from django.conf import settings
    _django_reference_backend()
    _django_test_app()
    ref = {"ENGINE": "compat_django_reference", "NAME": "unset.sqlite3"}
    s3 = {"ENGINE": "s3rds.sqlite", "NAME": "compat", "HOST": "in-process.test", "USER": "admin", "PASSWORD": "pw"}
    settings.configure(
        DATABASES={"default": ref, "ref": ref, "ref2": dict(ref), "s3": s3, "s3b": dict(s3)},
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "compat_django_app.apps.CompatConfig"],
        USE_TZ=True, TIME_ZONE="UTC", DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],  # fast; hashing is client-side anyway
    )
    django.setup()


class reference_types:
    """with reference_types(): Django's converters / adapters on the engine module, the server's put back after."""
    def __enter__(self):
        self.saved = dict(ENGINE.converters), dict(ENGINE.adapters)
        ENGINE.converters.clear()
        ENGINE.converters.update(_DJANGO_TYPES["converters"])
        ENGINE.adapters.update(_DJANGO_TYPES["adapters"])

    def __exit__(self, *exc):
        for registry, saved in zip((ENGINE.converters, ENGINE.adapters), self.saved):
            registry.clear()
            registry.update(saved)


try:  # here, before test_django.py defines its models
    import django  # noqa: F401
except ImportError:
    pass
else:
    _django_configure()


@pytest.fixture
def django_pair(fake_s3, monkeypatch, bucket, tmp_path):
    """run(scenario) -> scenario(db, other) on Django + sqlite3 (aliases ref, ref2) and on ENGINE s3rds.sqlite
    (s3, s3b); the returned values must be equal. run.unsupported(scenario): the reference runs it, s3rds
    refuses it with NotSupportedError."""
    django = pytest.importorskip("django")
    from types import SimpleNamespace

    from django.db import NotSupportedError, connections

    connect = s3sqlite.connect
    monkeypatch.setattr(s3sqlite, "connect", lambda host, user, password, database, **kw: in_process(
        SimpleNamespace(connect=connect), fake_s3, monkeypatch, bucket, **kw))
    for alias in ("ref", "ref2"):
        connections[alias].settings_dict["NAME"] = str(tmp_path / "ref.sqlite3")
    connections.close_all()
    if not compat_report.versions.get("django"):
        compat_report.versions["django"] = f"Django {django.get_version()}"

    def run(scenario):
        try:
            with reference_types():
                real = scenario("ref", "ref2")
            ours = scenario("s3", "s3b")
        finally:
            connections.close_all()
        assert real == ours, f"\n  real:  {real}\n  s3rds: {ours}"

    def unsupported(scenario):
        try:
            with reference_types():
                scenario("ref", "ref2")  # the reference runs it
            with pytest.raises(NotSupportedError):
                scenario("s3", "s3b")
        finally:
            connections.close_all()
    run.unsupported = unsupported
    yield run
    connections.close_all()
