"""s3rds server: a serverless database (SQL over HTTP, SQLite on S3).

GET  /                                      index.html (web console; CONSOLE=0 -> 404, API only)
GET  /api/databases                         list databases
POST /api/databases {name}                  create a database
GET  /api/db/{db}/tables                    list tables
GET  /api/db/{db}/table/{t}?limit&offset    browse rows (with rowid and structure)
POST /api/db/{db}/query {sql, params?}      run one SQL statement (parameters allowed)
POST /api/db/{db}/tx {statements:[{sql,params?},...]}
                                            several statements in one transaction: all commit or none
POST /api/db/{db}/replay {statements, replay, base?, commit}
                                            an open transaction of the driver: replay the log, run the rest,
                                            roll back (a look) or commit
Data calls may carry pragmas {foreign_keys: 0|1, legacy_alter_table: 0|1}: the driver's connection settings,
applied before BEGIN on every request (the server keeps no session).

Read / write is decided by the endpoint, never by guessing the SQL:
  write endpoint (default)   /query goes through run(): SQLite decides whether data changed, only then S3 is written
  read-only endpoint READONLY=1   everything opens mode=ro; a write is refused by SQLite -> 403
Auth: HTTP Basic, the same meaning as RDS user / password. No DB_USER / DB_PASSWORD -> every request 503, unless
ALLOW_NO_AUTH=1 (local experiments only), which answers without auth and says so in x-s3rds-warning: no-auth.
ATTACH / DETACH / VACUUM INTO -> 403: a request works on its own database file only.
Every JSON reply carries ms (server time); data calls also carry s3 (cache-hit / download / +upload).
"""
import base64
import hmac
import json
import os
import re
import time

from .s3_sqlite import (SESSION_PRAGMAS, DatabaseExists, DatabaseNotFound, NotAllowed, SqliteS3, WriteConflict,
                       create_database, list_databases, sqlite3)

BUCKET = os.environ["DB_BUCKET"]
PREFIX = os.environ.get("DB_PREFIX", "")
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
ALLOW_NO_AUTH = os.environ.get("ALLOW_NO_AUTH") == "1"  # no credentials configured: refuse (503) unless this is set
READONLY = os.environ.get("READONLY") == "1"
CONSOLE = os.environ.get("CONSOLE", "1") == "1"  # the web console at GET /; unset = on

with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()

NAME = r"[A-Za-z0-9_-]{1,64}"  # valid database / table names
NAME_RE = re.compile(NAME + r"$")
MAX_ROWS = 500           # console browse page size (UI only, not the driver)
PAGE_BYTES = 4 * 1024 * 1024  # /query page size in bytes, well under Lambda's 6 MB response limit


class BadRequest(Exception):
    pass


# ---------------- value encoding: what JSON cannot carry ----------------
# binary: {"$b": base64} both ways; type markers (only when the request says types:true) by declared column type:
# DATETIME/TIMESTAMP → {"$dt": text}, DATE → {"$d": text}, TIME → {"$t": text}, DECIMAL/NUMERIC → {"$n": text}

def _decode_param(v):
    if isinstance(v, dict) and "$b" in v:
        return base64.b64decode(v["$b"])
    return v


def _encode_cell(v):
    if isinstance(v, (bytes, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode()}
    return v


for _decl, _tag in (("DATETIME", "$dt"), ("TIMESTAMP", "$dt"), ("DATE", "$d"), ("TIME", "$t"),
                    ("DECIMAL", "$n"), ("NUMERIC", "$n")):
    sqlite3.register_converter(_decl, (lambda tag: lambda b: {tag: b.decode()})(_tag))


class Forbidden(Exception):
    pass


class VersionChanged(Exception):
    """The database changed while paging: two pages would be two versions, so refuse to mix them."""


class TxConflict(Exception):
    """An open transaction's base version is gone: someone else committed, so its reads may be stale."""


# ---------------- shared helpers ----------------

def _json(status, data):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(data, ensure_ascii=False, default=str),
    }


def _body(event):
    """The JSON request body; 400 before touching any data when it is invalid."""
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw)
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("request body is not valid JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("request body must be a JSON object")
    return data


def _int(qs, key, default, lo, hi):
    raw = qs.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{key} must be an integer") from None
    return max(lo, min(value, hi))


def _no_credentials():
    return not DB_USER or not DB_PASSWORD


def _authed(event):
    """HTTP Basic, constant-time compare. The handler answers 503 before this when no credentials are configured."""
    if _no_credentials():
        return ALLOW_NO_AUTH
    auth =(event.get("headers") or {}).get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(
            auth.split(None, 1)[1]).decode().partition(":")
    except Exception:
        return False
    return (hmac.compare_digest(user.encode(), DB_USER.encode())
            & hmac.compare_digest(password.encode(), DB_PASSWORD.encode()))


def _db(name):
    return SqliteS3(BUCKET, f"{PREFIX}{name}.sqlite")


def _s3_tag(db):
    return (("download" if db.stats["downloaded"] else "cache-hit")
            + ("+upload" if db.stats["uploaded"] else ""))


# ---------------- routes: return a dict (200) or (status, dict) ----------------

def list_dbs(event):
    return {"databases": list_databases(BUCKET, PREFIX)}


def create_db(event):
    if READONLY:
        raise Forbidden("the read-only endpoint cannot create databases; use the write endpoint")
    name = (_body(event).get("name") or "").strip()
    if not NAME_RE.fullmatch(name):
        raise BadRequest("database names: letters, digits, - and _ only, up to 64 characters")
    try:
        create_database(BUCKET, f"{PREFIX}{name}.sqlite")
    except DatabaseExists:
        return 409, {"error": f"database already exists: {name}"}
    return 201, {"created": name}


def list_tables(event, db):
    tables = _db(db).read(lambda c: [r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")])
    return {"tables": tables}


def browse(event, db, table):
    qs = event.get("queryStringParameters") or {}
    limit = _int(qs, "limit", 50, 1, MAX_ROWS)
    offset = _int(qs, "offset", 0, 0, 10 ** 9)

    def fn(c):
        names = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if table not in names:  # table names come from the catalogue, never from the request
            raise sqlite3.OperationalError(f"no such table: {table}")
        total = c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        try:
            cur = c.execute(
                f'SELECT rowid AS _rowid_, * FROM "{table}" LIMIT ? OFFSET ?',
                (limit, offset))
        except sqlite3.OperationalError:  # WITHOUT ROWID table: read-only browsing
            cur = c.execute(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (limit, offset))
        return {
            "columns": [d[0] for d in cur.description],
            "rows": [list(r) for r in cur.fetchall()],
            "total": total, "limit": limit, "offset": offset,
            # structure (like MySQL's DESCRIBE): name / type / notnull / dflt_value / pk
            "structure": [dict(r) for r in c.execute(f'PRAGMA table_info("{table}")')],
        }

    inst = _db(db)
    result = inst.read(fn)
    result["s3"] = _s3_tag(inst)
    return result


def _prepare(stmt, where="request"):
    """Validate one {sql, params} -> (sql, params); 400 before touching data when invalid."""
    if not isinstance(stmt, dict):
        raise BadRequest(f"{where} must be an object {{sql, params?}}")
    sql = (stmt.get("sql") or "").strip().rstrip(";")
    params = stmt.get("params") or []
    if not sql:
        raise BadRequest(f"{where}.sql must not be empty")
    if not isinstance(params, list):
        raise BadRequest(f"{where}.params must be a list")
    return sql, params


def _exec_one(c, sql, params, offset=0):
    """Run one statement; the result is paged by bytes: skip offset rows, fill up to PAGE_BYTES, has_more if rows remain."""
    cur = c.execute(sql, [_decode_param(p) for p in params])
    if not cur.description:
        return {"columns": [], "rows": [], "rowcount": cur.rowcount, "lastrowid": cur.lastrowid}
    skipped = 0
    while skipped < offset:
        chunk = cur.fetchmany(min(1000, offset - skipped))
        if not chunk:
            break
        skipped += len(chunk)
    rows, size, has_more = [], 0, False
    for row in cur:
        row = [_encode_cell(v) for v in row]
        rows.append(row)
        size += len(json.dumps(row, default=str)) + 2
        if size >= PAGE_BYTES:
            has_more = cur.fetchone() is not None
            break
    # rowcount as sqlite3 reports it once every row is read: -1 for SELECT, the rows changed for
    # INSERT / UPDATE / DELETE … RETURNING (SQLAlchemy checks it, e.g. for version_id_col)
    return {"columns": [d[0] for d in cur.description], "rows": rows,
            "rowcount": -1 if has_more else cur.rowcount,
            "lastrowid": None, "has_more": has_more, "next_offset": offset + len(rows)}


def _pragmas(body):
    """The connection settings the driver sends with every request: {"foreign_keys": 0 | 1, ...}."""
    pragmas = body.get("pragmas") or {}
    if not isinstance(pragmas, dict):
        raise BadRequest("pragmas must be an object")
    for name, value in pragmas.items():
        if name not in SESSION_PRAGMAS:
            raise BadRequest(f"pragmas: {name} is not a connection setting the server applies "
                             f"({', '.join(SESSION_PRAGMAS)})")
        if value not in (0, 1) or isinstance(value, bool):
            raise BadRequest(f"pragmas.{name} must be 0 or 1")
    return pragmas


def _run(inst, fn, typed=False, trial=False, pragmas=None):
    """read() or run() by endpoint (trial() for a look inside an open transaction), with timing and the S3 action;
    a write on the read-only endpoint is refused by SQLite -> 403."""
    t0 = time.perf_counter()
    detect = sqlite3.PARSE_DECLTYPES if typed else 0
    try:
        result = (inst.read(fn, detect, pragmas) if READONLY else
                  inst.trial(fn, detect, pragmas) if trial else inst.run(fn, detect, pragmas))
    except sqlite3.OperationalError as e:
        if READONLY and "readonly" in str(e):
            raise Forbidden("this is the read-only endpoint; send writes to the write endpoint") from None
        raise
    result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["s3"] = _s3_tag(inst)
    return result


def _token(sql, params, offset, etag, typed):
    return base64.b64encode(json.dumps(
        {"sql": sql, "params": params, "offset": offset, "etag": etag, "types": typed}).encode()).decode()


def query(event, db):
    """One SQL statement. A result over PAGE_BYTES carries next_token; the next page sends only next_token.
    The token carries the database version (ETag) at the time of the page; a changed version -> 409 VersionChanged, the client re-runs."""
    body = _body(event)
    token = body.get("next_token")
    if token:
        try:
            state = json.loads(base64.b64decode(token))
            sql, params, offset, want = state["sql"], state["params"], int(state["offset"]), state["etag"]
            typed = bool(state.get("types"))
        except Exception:
            raise BadRequest("invalid next_token") from None
    else:
        sql, params = _prepare(body)
        offset, want, typed = 0, None, bool(body.get("types"))

    inst = _db(db)

    def fn(c):
        if want is not None and inst.etag != want:
            raise VersionChanged()
        return _exec_one(c, sql, params, offset)

    result = _run(inst, fn, typed, pragmas=_pragmas(body))
    has_more = result.pop("has_more", False)
    next_offset = result.pop("next_offset", None)
    if has_more:
        result["next_token"] = _token(sql, params, next_offset, inst.etag, typed)
    result["truncated"] = has_more  # read by 0.1.0 clients; a new client raises on truncated without next_token
    return result


def tx(event, db):
    """Several statements, one transaction. SqliteS3.run is already one BEGIN/COMMIT;
    fn just runs them in order; any error -> ROLLBACK of everything, S3 untouched."""
    body = _body(event)
    return _statements(db, body, 0, None, True)


def replay(event, db):
    """The driver's open transactions (connect(autocommit=False)); the server keeps no session, the driver
    sends the transaction so far with every request:
      statements  the log (statements that may have written) followed by the new ones
      replay: n   the first n are the log: run again, results not returned
      base: etag  the version the transaction began on; any other version -> 409 TxConflict
      commit      false: roll back at the end (a look inside the transaction, S3 never written);
                  true: commit and upload, as /tx
    The reply carries etag, the version the statements ran on. A separate route, so that a server
    without it answers 404 instead of committing a look."""
    body = _body(event)
    n, base, commit = body.get("replay", 0), body.get("base"), body.get("commit")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise BadRequest("replay must be a non-negative integer")
    if base is not None and not isinstance(base, str):
        raise BadRequest("base must be an ETag string")
    if not isinstance(commit, bool):
        raise BadRequest("commit must be true or false")
    return _statements(db, body, n, base, commit)


def _statements(db, body, replayed, base, commit):
    stmts = body.get("statements")
    if not isinstance(stmts, list) or not stmts:
        raise BadRequest("statements must be a non-empty list: [{sql, params?}, ...]")
    if replayed > len(stmts):
        raise BadRequest("replay is larger than the number of statements")
    prepared = [_prepare(s, f"statements[{i}]") for i, s in enumerate(stmts)]
    inst = _db(db)

    def fn(c):
        if base is not None and inst.etag != base:
            raise TxConflict()
        results = []
        for i, (sql, params) in enumerate(prepared):
            r = _exec_one(c, sql, params)
            if i < replayed:
                continue
            if r.pop("has_more", False):  # no paging inside a transaction: an oversized result is an explicit error
                raise BadRequest(f"statements[{i}] result exceeds {PAGE_BYTES} bytes; run it as a plain query")
            r.pop("next_offset", None)
            results.append(r)
        return {"results": results, "etag": inst.etag}

    return _run(inst, fn, bool(body.get("types")), trial=not commit, pragmas=_pragmas(body))


ROUTES = [
    ("GET",  re.compile(r"/api/databases$"), list_dbs),
    ("POST", re.compile(r"/api/databases$"), create_db),
    ("GET",  re.compile(rf"/api/db/(?P<db>{NAME})/tables$"), list_tables),
    ("GET",  re.compile(rf"/api/db/(?P<db>{NAME})/table/(?P<table>{NAME})$"), browse),
    ("POST", re.compile(rf"/api/db/(?P<db>{NAME})/query$"), query),
    ("POST", re.compile(rf"/api/db/(?P<db>{NAME})/tx$"), tx),
    ("POST", re.compile(rf"/api/db/(?P<db>{NAME})/replay$"), replay),
]


# ---------------- entry point ----------------

def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    if method == "GET" and path in ("/", "/index.html"):
        if not CONSOLE:
            return _json(404, {"error": "the web console is turned off (console = false); the API is under /api/"})
        return {
            "statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": INDEX_HTML,
        }

    if _no_credentials() and not ALLOW_NO_AUTH:
        return _json(503, {"error": "server has no credentials configured (DB_USER / DB_PASSWORD)"})
    if not _authed(event):
        return _json(401, {"error": "bad credentials"})

    t0 = time.perf_counter()
    status, data = 404, {"error": f"no route: {method} {path}"}
    try:
        for route_method, pattern, handler in ROUTES:
            match = pattern.fullmatch(path)
            if match and route_method == method:
                out = handler(event, **match.groupdict())
                status, data = out if isinstance(out, tuple) else (200, out)
                break
    except BadRequest as e:
        status, data = 400, {"error": str(e)}
    except (Forbidden, NotAllowed) as e:
        status, data = 403, {"error": str(e)}
    except DatabaseNotFound as e:
        status, data = 404, {"error": f"database not found: {e}"}
    except WriteConflict as e:
        status, data = 409, {"error": str(e)}
    except VersionChanged:
        status, data = 409, {"error": "database version changed while paging; re-run the query"}
    except TxConflict:
        status, data = 409, {"error": "transaction conflict: another writer committed since this transaction "
                                      "began; roll back and run the transaction again"}
    except sqlite3.Error as e:
        status, data = 400, {"error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        status, data = 500, {"error": f"{type(e).__name__}: {e}"}

    data.setdefault("ms", round((time.perf_counter() - t0) * 1000, 1))
    resp = _json(status, data)
    if _no_credentials():  # here only with ALLOW_NO_AUTH=1
        resp["headers"]["x-s3rds-warning"] = "no-auth"
    return resp
