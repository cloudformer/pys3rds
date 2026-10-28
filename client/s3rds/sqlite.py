"""s3rds.sqlite — the native (SQLite-dialect) driver for s3rds: Lambda URL endpoint, SQLite on S3.

Standard library only. The API is shaped like pymysql
(connect(host=, user=, password=, database=), cursors, %s placeholders); the SQL dialect is SQLite.

    from s3rds import sqlite
    conn = sqlite.connect(host="xxx.lambda-url.us-east-2.on.aws",
                          user="admin", password="...", database="blog")

    with conn.cursor() as cur:                          # DB-API style
        cur.execute("INSERT INTO items (text) VALUES (%s)", ["hi"])
        new_id = cur.lastrowid
    with conn.cursor(sqlite.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM items WHERE id = %s", [new_id])
        rows = cur.fetchall()                           # [{"id": 1, "text": "hi"}]

    conn.execute("INSERT INTO items (text) VALUES (?)", ["hi"])   # convenience; ? and %s both accepted
    rows = conn.query("SELECT * FROM items")                       # [dict, ...]
    conn.transaction([("UPDATE ...", [..]), ("INSERT ...", [..])])  # one transaction, one S3 write
    conn.last                                                      # {"ms": server time, "s3": "cache-hit"...}

Transactions: by default every execute() is its own transaction and is durable in S3 when it returns
(= pymysql autocommit=True); transaction() runs several atomically, commit()/rollback() are no-ops.
connect(..., autocommit=False) gives sqlite3's transactions: INSERT / UPDATE / DELETE open one, reads see
its own writes, commit() writes S3 once, rollback() discards; another writer committing in between makes
the next statement or commit() raise OperationalError (roll back and run it again).

SQLAlchemy: create_engine("sqlite+s3rds://user:password@xxx.lambda-url.us-east-2.on.aws/blog")
SQLAlchemy's SQLite dialect with this driver, autocommit=False; SQLAlchemy itself is the application's.

Retry policy -- fail rather than duplicate a write:
  the request is re-sent exactly once, and only when it provably never reached the server
  (an idle keep-alive connection closed by the peer). Timeouts and anything after the request
  was sent raise Error with "result unknown"; the caller decides.
(A connection is not thread-safe; use one per thread.)
"""
import base64
import datetime
import decimal
import http.client
import json
import re
import socket
import urllib.parse
import warnings

__version__ = "0.2.0"

apilevel = "2.0"
threadsafety = 1
paramstyle = "format"  # %s like pymysql; ? is accepted too
# the engine behind the endpoint: the Lambda runtime's SQLite (tests/compat/TASKS.md, task 11);
# SQLAlchemy reads it to decide what SQL it may use (RETURNING needs 3.35)
sqlite_version = "3.40.0"
sqlite_version_info = (3, 40, 0)
Binary = memoryview  # as sqlite3


# ---------------- exceptions: the DB-API classes sqlite3 raises ----------------

class Warning(Exception):  # noqa: A001  (DB-API name, as sqlite3.Warning)
    pass


class Error(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status  # HTTP status; 0 = network layer
        self.message = message


class InterfaceError(Error): pass
class DatabaseError(Error): pass
class DataError(DatabaseError): pass
class OperationalError(DatabaseError): pass
class IntegrityError(DatabaseError): pass
class InternalError(DatabaseError): pass
class ProgrammingError(DatabaseError): pass
class NotSupportedError(DatabaseError): pass


_BY_NAME = {c.__name__: c for c in (InterfaceError, DatabaseError, DataError, OperationalError,
                                    IntegrityError, InternalError, ProgrammingError, NotSupportedError)}


def _error(status, message):
    """A failed reply -> the class sqlite3 would have raised. SQLite errors arrive as "IntegrityError: ...";
    the rest by status: 400 a bad request, 401/403/404/409 and the network (0) operational, 5xx internal."""
    cls = _BY_NAME.get((message or "").split(":", 1)[0])
    if cls is None:
        cls = (ProgrammingError if status == 400 else InternalError if status >= 500 else OperationalError)
    return cls(status, message)


class _NotDelivered(Exception):
    """The request provably never reached the server: sending failed, or no status line came back.

    Only this case may be retried; any failure while reading the response is not --
    the server may already have executed the statement.
    """
    def __init__(self, cause, reused):
        super().__init__(str(cause))
        self.cause = cause
        self.reused = reused  # happened on a reused keep-alive connection (idle connection closed by peer)


# ---------------- parameters: Python values -> what JSON (and SQLite) can carry ----------------

def _encode_value(value, index):
    if value is None or isinstance(value, (int, float, str)) and not isinstance(value, bool):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, datetime.datetime):  # before date: datetime is a date subclass
        text = value.strftime("%Y-%m-%d %H:%M:%S")
        return f"{text}.{value.microsecond:06d}" if value.microsecond else text
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        text = value.strftime("%H:%M:%S")
        return f"{text}.{value.microsecond:06d}" if value.microsecond else text
    if isinstance(value, decimal.Decimal):
        return str(value)  # keeps the precision; never a float
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode()}
    raise ProgrammingError(0, f"parameter #{index}: type {type(value).__name__} cannot be encoded")


def _encode_params(params):
    return [_encode_value(v, i + 1) for i, v in enumerate(params or [])]


# ---------------- results: the server's markers -> Python values ----------------
# {"$b": base64} is always sent for BLOBs; the typed markers only when the connection asks for
# types (the MySQL driver does; this driver returns text exactly as sqlite3 would).

def _decode_cell(v):
    if not isinstance(v, dict) or len(v) != 1:
        return v
    (tag, text), = v.items()
    if tag == "$b":
        return base64.b64decode(text)
    if tag == "$dt":
        return datetime.datetime.fromisoformat(text)
    if tag == "$d":
        return datetime.date.fromisoformat(text)
    if tag == "$t":
        return datetime.time.fromisoformat(text)
    if tag == "$n":
        return decimal.Decimal(text)
    return v


def _decode_result(r):
    if r.get("rows"):
        r["rows"] = [[_decode_cell(v) for v in row] for row in r["rows"]]
    return r


# ---------------- placeholders: %s (pymysql style) -> ? (SQLite) ----------------

def _to_qmark(sql):
    """Turn %s into ? and %% into % outside quotes; leave quoted text alone. ?-style SQL is returned as is."""
    if "%" not in sql:
        return sql
    out, i, n, quote = [], 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
        elif ch == "%" and i + 1 < n and sql[i + 1] == "s":
            out.append("?")
            i += 1
        elif ch == "%" and i + 1 < n and sql[i + 1] == "%":
            out.append("%")
            i += 1
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# ---------------- cursors (DB-API 2.0 subset, enough for common pymysql usage) ----------------

class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1
        self._rows = []
        self._pos = 0

    def _row(self, columns, row):
        return tuple(row)

    def execute(self, sql, params=None):
        r = self.connection.execute(sql, params or [])
        cols = r.get("columns") or []
        self.description = [(c, None, None, None, None, None, None) for c in cols] or None
        self.rowcount = r.get("rowcount", -1)
        self.lastrowid = r.get("lastrowid")
        self._rows = [self._row(cols, row) for row in r.get("rows") or []]
        self._pos = 0
        return self.rowcount

    def executemany(self, sql, seq_of_params):
        """Same statement, several parameter sets, one transaction (all or nothing) -- or, with
        autocommit=False, part of the open transaction.

        rowcount = rows actually affected, summed; lastrowid = the last statement's.
        """
        results = self.connection._executemany(sql, seq_of_params)
        self.rowcount = sum(max(r.get("rowcount", 0), 0) for r in results)
        self.lastrowid = results[-1].get("lastrowid") if results else None
        self.description = None
        self._rows, self._pos = [], 0
        return self.rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchmany(self, size=None):
        size = size or self.arraysize
        rows = self._rows[self._pos:self._pos + size]
        self._pos += len(rows)
        return rows

    def fetchall(self):
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self):
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class DictCursor(Cursor):
    def _row(self, columns, row):
        return dict(zip(columns, row))


class cursors:  # so that sqlite.cursors.DictCursor reads like pymysql.cursors.DictCursor
    Cursor = Cursor
    DictCursor = DictCursor


# ---------------- connection ----------------

class Connection:
    _typed = False   # ask the server for column-type markers (the MySQL driver does; sqlite3 returns text)

    def __init__(self, url, database, user="", password="", timeout=30, cursorclass=Cursor, autocommit=True):
        p = urllib.parse.urlparse(url.rstrip("/"))
        self._host = p.netloc
        self._base_path = p.path
        self.database = database
        self.cursorclass = cursorclass
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout
        self._conn = None  # reused HTTPS keep-alive connection
        # server-side metrics of the last call: {"ms": ..., "s3": "cache-hit" | "download" | "...+upload"}
        self.last = {}
        self._written = False  # a write landed in S3 since the last commit() (drives the rollback() warning)
        self._autocommit = autocommit
        # the open transaction (autocommit=False): {"log": statements that may have written, "base": ETag}
        self._tx = None

    # ---- transport ----

    def _drop(self):
        try:
            if self._conn:
                self._conn.close()
        finally:
            self._conn = None

    def _send(self, path, body):
        reused = self._conn is not None
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self._host, timeout=self.timeout)
        headers = {"content-type": "application/json",
                   "authorization": f"Basic {self._auth}"}
        # phase 1: send. A timeout is NOT "not delivered" (the body may have arrived in full)
        try:
            self._conn.request("POST", self._base_path + path, body, headers)
        except (socket.timeout, TimeoutError):
            raise
        except (OSError, http.client.HTTPException) as e:
            raise _NotDelivered(e, reused) from None
        # phase 2: status line. Nothing received at all -> the server did not process it
        try:
            resp = self._conn.getresponse()
        except (http.client.RemoteDisconnected, http.client.BadStatusLine) as e:
            raise _NotDelivered(e, reused) from None
        # phase 3: body. Any error here is never retried -- the server most likely executed it
        raw = resp.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = {"error": raw[:200].decode(errors="replace")}
        if resp.status >= 400:
            raise _error(resp.status, data.get("error", ""))
        return data

    def _post(self, path, payload):
        body = json.dumps(payload)
        try:
            return self._send(path, body)
        except _NotDelivered as e:
            self._drop()
            if not e.reused:  # a fresh connection failed: not a stale keep-alive, do not guess
                raise OperationalError(0, f"connection failed: {e.cause}") from None
            try:
                return self._send(path, body)  # the only safe retry: provably not delivered
            except _NotDelivered as e2:
                self._drop()
                raise OperationalError(0, f"connection failed: {e2.cause}") from None
        except (socket.timeout, TimeoutError):
            self._drop()
            raise OperationalError(0, "timeout: result unknown (the statement may have run); do not blindly retry writes") from None
        except (OSError, http.client.HTTPException) as e:
            self._drop()
            raise OperationalError(0, f"network error (result unknown): {e}") from None

    def _get(self, path):
        """A GET on a short-lived connection (the keep-alive path is POST-only)."""
        conn = http.client.HTTPSConnection(self._host, timeout=self.timeout)
        try:
            conn.request("GET", self._base_path + path, headers={"authorization": f"Basic {self._auth}"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
            if resp.status >= 400:
                raise _error(resp.status, data.get("error", ""))
            return data
        finally:
            conn.close()

    # ---- DB-API ----

    def cursor(self, cursorclass=None):
        return (cursorclass or self.cursorclass)(self)

    def commit(self):
        """autocommit=False: the open transaction's writes commit together, S3 is written once; a conflict
        with another writer raises OperationalError and nothing lands.
        autocommit=True: every statement is already committed and durable when execute() returns; this only
        clears the 'written since last commit' flag (kept for pymysql compatibility)."""
        tx, self._tx = self._tx, None
        if tx and tx["log"]:
            payload = {"statements": tx["log"], "replay": len(tx["log"]), "base": tx["base"], "commit": True}
            self._note(self._replay(payload))
        self._written = False

    def rollback(self):
        """autocommit=False: the open transaction never reached S3; forgetting it is the rollback.
        autocommit=True: cannot undo anything, statements are durable as soon as they run. If something was
        written since the last commit() this warns (never silent); with nothing written it stays quiet,
        because pools and ORMs call rollback() when returning a connection."""
        self._tx = None
        if self._written and self._autocommit:
            warnings.warn("s3rds: rollback() cannot undo statements that already ran; "
                          "use transaction() for all-or-nothing writes", RuntimeWarning, stacklevel=2)
        self._written = False

    @property
    def isolation_level(self):
        """As sqlite3: None = autocommit, "" = transactions (implicit BEGIN before INSERT / UPDATE / DELETE /
        REPLACE). Setting None commits the open transaction, as sqlite3 does."""
        return None if self._autocommit else ""

    @isolation_level.setter
    def isolation_level(self, value):
        if value is None and self._tx is not None:
            self.commit()
        self._autocommit = value is None

    @property
    def in_transaction(self):
        return self._tx is not None

    def create_function(self, *a, **kw):
        raise NotSupportedError(0, "user-defined functions would run on the server; "
                                   "not supported (that is the moment to move to RDS)")

    def close(self):
        self._tx = None  # as sqlite3: closing without commit() discards the open transaction
        self._drop()

    def ping(self, reconnect=True):
        self.execute("SELECT 1")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- convenience ----

    def create_database(self):
        """Create the database; a 409 (already exists) is silently fine, so this is safe on cold start."""
        try:
            self._post("/api/databases", {"name": self.database})
        except Error as e:
            if e.status != 409:
                raise

    def list_databases(self):
        return self._get("/api/databases").get("databases", [])

    def execute(self, sql, params=()):
        """Run one statement (? or %s placeholders). Returns {columns, rows, rowcount, lastrowid, ms, s3}.

        Large results come back from the server in pages; all pages are fetched here, so `rows` is
        always complete. If the database changes while paging the server refuses to mix versions
        (409) and the whole query is re-run, up to 3 times.

        autocommit=False follows sqlite3's rules: INSERT / UPDATE / DELETE / REPLACE (and SAVEPOINT) open a
        transaction; BEGIN / COMMIT / ROLLBACK statements act as begin / commit() / rollback(); anything
        else outside a transaction runs on its own.
        """
        if not self._autocommit:
            r = self._tx_statement(sql, params)
            if r is not None:
                return r
        path = f"/api/db/{self.database}/query"
        payload = {"sql": _to_qmark(sql), "params": _encode_params(params)}
        if self._typed:
            payload["types"] = True
        for attempt in range(3):
            try:
                return self._fetch_pages(path, payload)
            except Error as e:
                if e.status == 409 and "version changed" in e.message and attempt < 2:
                    continue
                raise

    def _fetch_pages(self, path, payload):
        first = self._post(path, payload)
        if first.get("truncated") and not first.get("next_token"):
            raise InterfaceError(0, "the server truncated the result (server older than 0.1.1); upgrade the server")
        rows, page = first["rows"], first
        while page.get("next_token"):
            page = self._post(path, {"next_token": page["next_token"]})
            rows += page["rows"]
        first["rows"] = rows
        first.pop("truncated", None)
        first.pop("next_token", None)
        self._note(first)
        return _decode_result(first)

    def _note(self, r):
        self.last = {"ms": r.get("ms"), "s3": r.get("s3")}
        if "+upload" in (r.get("s3") or ""):  # the server only uploads when data actually changed
            self._written = True

    def query(self, sql, params=()):
        """SELECT helper returning the complete result as [dict, ...]."""
        r = self.execute(sql, params)
        return [dict(zip(r["columns"], row)) for row in r["rows"]]

    def transaction(self, statements):
        """Run several statements in one transaction: all commit together or none do, and S3 is written once.

        Each item is "sql" or (sql, params). Returns one result per statement.
        """
        stmts = [_statement(s, []) if isinstance(s, str) else _statement(s[0], s[1] if len(s) > 1 else [])
                 for s in statements]
        payload = {"statements": stmts}
        if self._typed:
            payload["types"] = True
        r = self._post(f"/api/db/{self.database}/tx", payload)
        self._note(r)
        return [_decode_result(x) for x in r["results"]]

    # ---- open transactions (autocommit=False) ----
    # The server keeps no session: every statement inside a transaction is sent with the log of the
    # statements that may have written so far. The server replays the log on the version the transaction
    # began on (base), runs the new statements, returns their results and rolls back; commit() sends the log
    # once more for real. Reads see the transaction's own writes; if anyone else committed in between, the
    # base is gone and the next statement or commit() raises OperationalError: roll back, run it again.

    def _tx_statement(self, sql, params):
        """sqlite3's rules for one statement; returns its result, or None when it runs on its own."""
        if _BEGIN.match(sql):
            if self._tx is not None:
                raise OperationalError(0, "cannot start a transaction within a transaction")
            self._tx = {"log": [], "base": None}
            return _empty()
        if _COMMIT.match(sql):
            self.commit()
            return _empty()
        if _ROLLBACK.match(sql):
            self.rollback()
            return _empty()
        if self._tx is None:
            if not _IMPLICIT_BEGIN.match(sql):
                return None
            self._tx = {"log": [], "base": None}
        return self._tx_run([(sql, params)])[0]

    def _executemany(self, sql, seq_of_params):
        statements = [(sql, p) for p in seq_of_params]
        if not statements:
            return []
        if self._autocommit or (self._tx is None and not _IMPLICIT_BEGIN.match(sql)):
            return self.transaction(statements)
        if self._tx is None:
            self._tx = {"log": [], "base": None}
        return self._tx_run(statements)

    def _tx_run(self, statements):
        tx = self._tx
        new = [_statement(sql, params) for sql, params in statements]
        payload = {"statements": tx["log"] + new, "replay": len(tx["log"]), "commit": False}
        if tx["base"]:
            payload["base"] = tx["base"]
        r = self._replay(payload)
        tx["base"] = tx["base"] or r.get("etag")
        tx["log"] += [s for s in new if not _READS.match(s["sql"])]
        self._note(r)
        return [_decode_result(x) for x in r["results"]]

    def _replay(self, payload):
        if self._typed:
            payload["types"] = True
        try:
            return self._post(f"/api/db/{self.database}/replay", payload)
        except Error as e:
            if e.status == 404 and "no route" in e.message:
                raise OperationalError(404, "the server has no open transactions (older than this driver); "
                                            "redeploy the server") from None
            raise


_BEGIN = re.compile(r"\s*BEGIN\b", re.I)
_COMMIT = re.compile(r"\s*(COMMIT|END)\b", re.I)
_ROLLBACK = re.compile(r"\s*ROLLBACK\b(?!.*\bTO\b)", re.I | re.S)  # ROLLBACK TO <savepoint> is a statement
_IMPLICIT_BEGIN = re.compile(r"\s*(INSERT|UPDATE|DELETE|REPLACE|SAVEPOINT)\b", re.I)
_READS = re.compile(r"\s*(SELECT|VALUES|EXPLAIN)\b", re.I)  # cannot write: never replayed


def _statement(sql, params):
    return {"sql": _to_qmark(sql), "params": _encode_params(params)}


def _empty():
    return {"columns": [], "rows": [], "rowcount": -1, "lastrowid": None}


# ---------------- module entry point, same signature as pymysql.connect ----------------

def connect(host, user="", password="", database="", timeout=30,
            cursorclass=Cursor, autocommit=True, **_ignored):
    """Extra pymysql arguments (port, charset, ...) are accepted and ignored.

    host may be given with or without https:// (Lambda Function URLs are https only).
    autocommit=True (default): every statement commits on its own. autocommit=False: transactions as
    sqlite3 has them -- commit() / rollback(), reads see the transaction's own writes.
    """
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, database, user, password, timeout, cursorclass, autocommit)


# ---------------- SQLAlchemy: sqlite+s3rds://user:password@<lambda-url>/<database> ----------------

def __getattr__(name):
    """PEP 562: the dialect is built when SQLAlchemy asks for it, so SQLAlchemy stays optional."""
    if name == "SQLAlchemyDialect":
        global SQLAlchemyDialect
        SQLAlchemyDialect = _sqlalchemy_dialect()
        return SQLAlchemyDialect
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _sqlalchemy_dialect():
    """SQLAlchemy's own SQLite dialect (it writes SQLite SQL; nothing is translated) with this module as the
    DB-API driver in place of the standard library's sqlite3."""
    import sys

    from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite
    from sqlalchemy.pool import QueuePool

    module = sys.modules[__name__]

    class SQLAlchemyDialect(SQLiteDialect_pysqlite):
        driver = "s3rds"
        supports_statement_cache = True

        def __init__(self, *a, **kw):
            kw.setdefault("paramstyle", "qmark")  # the module says format (pymysql style); SQLite's own is qmark
            super().__init__(*a, **kw)

        @classmethod
        def import_dbapi(cls):
            return module

        @classmethod
        def get_pool_class(cls, url):
            return QueuePool

        def create_connect_args(self, url):
            host = url.host + (f":{url.port}" if url.port else "")
            return [], {"host": host, "user": url.username or "", "password": url.password or "",
                        "database": url.database or "", "autocommit": False}

        def on_connect(self):
            return None  # pysqlite registers Python functions (regexp, floor) here; they cannot run on the server

        def set_isolation_level(self, dbapi_connection, level):
            dbapi_connection.isolation_level = None if level == "AUTOCOMMIT" else ""

        def get_isolation_level(self, dbapi_connection):
            return "SERIALIZABLE"  # a transaction fails on conflict rather than read another writer's data

    return SQLAlchemyDialect
