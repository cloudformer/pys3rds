"""pys3sqlite — the SQLite-dialect driver for pys3rds (a serverless RDS: Lambda URL endpoint, SQLite on S3).

Zero dependencies (standard library only). The interface is shaped like pymysql
(connect(host=, user=, password=, database=), cursors, %s placeholders), the SQL dialect is SQLite.
Same family, swap by changing one import line:

    import pys3sqlite        # today: SQLite dialect on the pys3rds endpoint
    import pys3mysql         # next:  MySQL dialect on the same endpoint
    import pymysql           # later: a real RDS MySQL

    conn = pys3sqlite.connect(
        host="xxx.lambda-url.us-east-2.on.aws",
        user="admin", password="...", database="blog")

    # DB-API style (like pymysql, placeholders %s)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO items (text) VALUES (%s)", ["hi"])
        new_id = cur.lastrowid
    with conn.cursor(pys3sqlite.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM items WHERE id = %s", [new_id])
        rows = cur.fetchall()                        # [{"id": 1, "text": "hi"}]

    # convenience methods (? and %s both accepted)
    conn.execute("INSERT INTO items (text) VALUES (?)", ["hi"])
    rows = conn.query("SELECT * FROM items")          # [dict, ...]
    conn.transaction([("UPDATE ...", [..]), ("INSERT ...", [..])])  # one transaction
    conn.last                                         # {"ms": server time, "s3": "cache-hit"...}

Transactions: every execute() is its own transaction and is durable in S3 when it returns
(= pymysql autocommit=True). Use transaction() for multi-statement atomicity.
commit()/rollback() exist for compatibility and are no-ops.

Retry policy — fail rather than duplicate a write:
  the request is re-sent exactly once, and only when it provably never reached the server
  (an idle keep-alive connection closed by the peer). Timeouts and anything after the request
  was sent raise Error with "result unknown"; the caller decides.
(A connection is not thread-safe; use one per thread.)
"""
import base64
import http.client
import json
import socket
import urllib.parse

__version__ = "0.1.0"

apilevel = "2.0"
threadsafety = 1
paramstyle = "format"  # %s like pymysql; ? is accepted too


# ---------------- exceptions ----------------

class Error(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status  # HTTP status; 0 = network layer
        self.message = message


SrdsError = Error  # old name


class _NotDelivered(Exception):
    """The request provably never reached the server: sending failed, or no status line came back.

    Only this case may be retried; any failure while reading the response is not —
    the server may already have executed the statement.
    """
    def __init__(self, cause, reused):
        super().__init__(str(cause))
        self.cause = cause
        self.reused = reused  # happened on a reused keep-alive connection (idle connection closed by peer)


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
        """Same statement, several parameter sets, one transaction (all or nothing)."""
        self.connection.transaction([(sql, p) for p in seq_of_params])
        self.rowcount = len(seq_of_params)
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


class cursors:  # so that pys3sqlite.cursors.DictCursor reads like pymysql
    Cursor = Cursor
    DictCursor = DictCursor


# ---------------- connection ----------------

class Connection:
    def __init__(self, url, database, user="", password="", timeout=30, cursorclass=Cursor):
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
            raise Error(resp.status, data.get("error", ""))
        return data

    def _post(self, path, payload):
        body = json.dumps(payload)
        try:
            return self._send(path, body)
        except _NotDelivered as e:
            self._drop()
            if not e.reused:  # a fresh connection failed: not a stale keep-alive, do not guess
                raise Error(0, f"connection failed: {e.cause}") from None
            try:
                return self._send(path, body)  # the only safe retry: provably not delivered
            except _NotDelivered as e2:
                self._drop()
                raise Error(0, f"connection failed: {e2.cause}") from None
        except (socket.timeout, TimeoutError):
            self._drop()
            raise Error(0, "timeout: result unknown (the statement may have run); do not blindly retry writes") from None
        except (OSError, http.client.HTTPException) as e:
            self._drop()
            raise Error(0, f"network error (result unknown): {e}") from None

    # ---- DB-API ----

    def cursor(self, cursorclass=None):
        return (cursorclass or self.cursorclass)(self)

    def commit(self):
        """Every statement is already committed and durable; kept for pymysql compatibility."""

    def rollback(self):
        """Nothing pending to roll back; kept for compatibility. Use transaction() for multi-statement atomicity."""

    def close(self):
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

    def execute(self, sql, params=()):
        """Run one statement (? or %s placeholders). Returns {columns, rows, rowcount, lastrowid, truncated, ms, s3}."""
        r = self._post(f"/api/db/{self.database}/query",
                       {"sql": _to_qmark(sql), "params": list(params)})
        self.last = {"ms": r.get("ms"), "s3": r.get("s3")}
        return r

    def query(self, sql, params=()):
        """SELECT helper returning [dict, ...]. Results over 500 rows are truncated (see execute()['truncated'])."""
        r = self.execute(sql, params)
        return [dict(zip(r["columns"], row)) for row in r["rows"]]

    def transaction(self, statements):
        """Run several statements in one transaction: all commit together or none do, and S3 is written once.

        Each item is "sql" or (sql, params). Returns one result per statement.
        """
        stmts = [{"sql": _to_qmark(s), "params": []} if isinstance(s, str)
                 else {"sql": _to_qmark(s[0]), "params": list(s[1]) if len(s) > 1 else []}
                 for s in statements]
        r = self._post(f"/api/db/{self.database}/tx", {"statements": stmts})
        self.last = {"ms": r.get("ms"), "s3": r.get("s3")}
        return r["results"]


SrdsClient = Connection  # old name


# ---------------- module entry point, same signature as pymysql.connect ----------------

def connect(host, user="", password="", database="", timeout=30,
            cursorclass=Cursor, **_ignored):
    """Extra pymysql arguments (port, charset, autocommit, ...) are accepted and ignored.

    host may be given with or without https:// (Lambda Function URLs are https only).
    """
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, database, user, password, timeout, cursorclass)
