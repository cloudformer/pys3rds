"""srds db:通用 serverless 数据库(SQL over HTTP + SQLite on S3)。

GET  /                                      index.html(web 控制台)
GET  /api/databases                         列库
POST /api/databases {name}                  建库
GET  /api/db/{db}/tables                    列表
GET  /api/db/{db}/table/{t}?limit&offset    浏览行(带 rowid 和表结构)
POST /api/db/{db}/query {sql, params?}      执行单条 SQL(可参数化)
POST /api/db/{db}/tx {statements:[{sql,params?},...]}
                                            多条 SQL 在一个事务里:全成才提交落盘,任一失败整体回滚

读写分离由入口决定,不猜 SQL:
  写端点(默认)        /query 走 run():SQLite 判定真改了数据才回传 S3
  只读端点 READONLY=1  一切以 mode=ro 打开,写会被 SQLite 拒绝 → 403
鉴权:HTTP Basic,语义对齐 RDS 的 user/password。
所有 JSON 响应都带 ms(本次服务端耗时);读写接口还带 s3(cache-hit/download/+upload)。
"""
import base64
import hmac
import json
import os
import re
import sqlite3
import time

from s3_sqlite import (DatabaseExists, DatabaseNotFound, SqliteS3, WriteConflict,
                       create_database, list_databases)

BUCKET = os.environ["DB_BUCKET"]
PREFIX = os.environ.get("DB_PREFIX", "")
DB_USER = os.environ.get("DB_USER", "")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
READONLY = os.environ.get("READONLY") == "1"

with open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8") as f:
    INDEX_HTML = f.read()

NAME = r"[A-Za-z0-9_-]{1,64}"  # 库名/表名共用的合法模式
NAME_RE = re.compile(NAME + r"$")
MAX_ROWS = 500           # 控制台 browse 单页行数上限(UI 习惯,与驱动无关)
PAGE_BYTES = 4 * 1024 * 1024  # /query 结果单页字节上限,留足 Lambda 6MB 响应余量


class BadRequest(Exception):
    pass


# ---------------- 值的编码:JSON 之外的类型 ----------------
# 二进制:{"$b": base64} 双向;类型标记(仅当请求带 types:true):按列的声明类型,
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
    """翻页时数据库已被写入:上一页和下一页不是同一个版本,拒绝混着返回。"""


# ---------------- 基础工具 ----------------

def _json(status, data):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(data, ensure_ascii=False, default=str),
    }


def _body(event):
    """解析 JSON 请求体;不合法直接 400,不碰任何数据。"""
    raw = event.get("body") or "{}"
    try:
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw)
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("请求体不是合法 JSON") from None
    if not isinstance(data, dict):
        raise BadRequest("请求体必须是 JSON 对象")
    return data


def _int(qs, key, default, lo, hi):
    raw = qs.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise BadRequest(f"{key} 必须是整数") from None
    return max(lo, min(value, hi))


def _authed(event):
    """HTTP Basic;恒定时间比较。没配 DB_USER = 不设防(别这么用)。"""
    if not DB_USER:
        return True
    auth = (event.get("headers") or {}).get("authorization", "")
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


# ---------------- 各接口:返回 dict(200) 或 (status, dict) ----------------

def list_dbs(event):
    return {"databases": list_databases(BUCKET, PREFIX)}


def create_db(event):
    if READONLY:
        raise Forbidden("只读入口不能建库,请走写端点")
    name = (_body(event).get("name") or "").strip()
    if not NAME_RE.fullmatch(name):
        raise BadRequest("库名只能是字母数字 - _,最长 64")
    try:
        create_database(BUCKET, f"{PREFIX}{name}.sqlite")
    except DatabaseExists:
        return 409, {"error": f"库已存在: {name}"}
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
        if table not in names:  # 表名白名单,防注入
            raise sqlite3.OperationalError(f"no such table: {table}")
        total = c.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        try:
            cur = c.execute(
                f'SELECT rowid AS _rowid_, * FROM "{table}" LIMIT ? OFFSET ?',
                (limit, offset))
        except sqlite3.OperationalError:  # WITHOUT ROWID 表:退化为只读浏览
            cur = c.execute(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (limit, offset))
        return {
            "columns": [d[0] for d in cur.description],
            "rows": [list(r) for r in cur.fetchall()],
            "total": total, "limit": limit, "offset": offset,
            # 表结构(同 MySQL 的 DESCRIBE):name/type/notnull/dflt_value/pk
            "structure": [dict(r) for r in c.execute(f'PRAGMA table_info("{table}")')],
        }

    inst = _db(db)
    result = inst.read(fn)
    result["s3"] = _s3_tag(inst)
    return result


def _prepare(stmt, where="请求"):
    """校验一条 {sql, params} → (sql, params);不合法直接 400,不碰数据。"""
    if not isinstance(stmt, dict):
        raise BadRequest(f"{where} 必须是对象 {{sql, params?}}")
    sql = (stmt.get("sql") or "").strip().rstrip(";")
    params = stmt.get("params") or []
    if not sql:
        raise BadRequest(f"{where}.sql 不能为空")
    if not isinstance(params, list):
        raise BadRequest(f"{where}.params 必须是数组")
    return sql, params


def _exec_one(c, sql, params, offset=0):
    """执行一条;结果集按字节分页:跳过 offset 行后最多装 PAGE_BYTES,还有剩就 has_more。"""
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
    return {"columns": [d[0] for d in cur.description], "rows": rows, "rowcount": -1,
            "lastrowid": None, "has_more": has_more, "next_offset": offset + len(rows)}


def _run(inst, fn, typed=False):
    """按入口选 read/run,附带耗时和 S3 动作;只读入口上的写被 SQLite 拒绝 → 403。"""
    t0 = time.perf_counter()
    detect = sqlite3.PARSE_DECLTYPES if typed else 0
    try:
        result = inst.read(fn, detect) if READONLY else inst.run(fn, detect)
    except sqlite3.OperationalError as e:
        if READONLY and "readonly" in str(e):
            raise Forbidden("这是只读入口,写操作请走写端点") from None
        raise
    result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    result["s3"] = _s3_tag(inst)
    return result


def _token(sql, params, offset, etag, typed):
    return base64.b64encode(json.dumps(
        {"sql": sql, "params": params, "offset": offset, "etag": etag, "types": typed}).encode()).decode()


def query(event, db):
    """一条 SQL。结果超过 PAGE_BYTES 时带 next_token;下一页只传 next_token。
    token 里带着发页时的数据库版本(ETag),翻页时版本变了 → 409 VersionChanged,客户端从头重跑。"""
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

    result = _run(inst, fn, typed)
    has_more = result.pop("has_more", False)
    next_offset = result.pop("next_offset", None)
    if has_more:
        result["next_token"] = _token(sql, params, next_offset, inst.etag, typed)
    result["truncated"] = has_more  # 0.1.0 客户端看的字段;新客户端见到 truncated 且无 next_token 会报错
    return result


def tx(event, db):
    """多条语句一个事务。SqliteS3.run 本身就是一个 BEGIN/COMMIT,
    这里只是把 fn 换成"顺序执行多条";任一条抛错 → 整体 ROLLBACK,S3 不动。"""
    body = _body(event)
    stmts = body.get("statements")
    if not isinstance(stmts, list) or not stmts:
        raise BadRequest("statements 必须是非空数组:[{sql, params?}, ...]")
    prepared = [_prepare(s, f"statements[{i}]") for i, s in enumerate(stmts)]

    def fn(c):
        results = []
        for i, (sql, params) in enumerate(prepared):
            r = _exec_one(c, sql, params)
            if r.pop("has_more", False):  # 事务里不分页:结果超页大小就明确报错
                raise BadRequest(f"statements[{i}] result exceeds {PAGE_BYTES} bytes; run it as a plain query")
            r.pop("next_offset", None)
            results.append(r)
        return {"results": results}

    return _run(_db(db), fn, bool(body.get("types")))


ROUTES = [
    ("GET",  re.compile(r"/api/databases$"), list_dbs),
    ("POST", re.compile(r"/api/databases$"), create_db),
    ("GET",  re.compile(rf"/api/db/(?P<db>{NAME})/tables$"), list_tables),
    ("GET",  re.compile(rf"/api/db/(?P<db>{NAME})/table/(?P<table>{NAME})$"), browse),
    ("POST", re.compile(rf"/api/db/(?P<db>{NAME})/query$"), query),
    ("POST", re.compile(rf"/api/db/(?P<db>{NAME})/tx$"), tx),
]


# ---------------- 入口 ----------------

def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    if method == "GET" and path in ("/", "/index.html"):
        return {
            "statusCode": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": INDEX_HTML,
        }

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
    except Forbidden as e:
        status, data = 403, {"error": str(e)}
    except DatabaseNotFound as e:
        status, data = 404, {"error": f"database not found: {e}"}
    except WriteConflict as e:
        status, data = 409, {"error": str(e)}
    except VersionChanged:
        status, data = 409, {"error": "database version changed while paging; re-run the query"}
    except sqlite3.Error as e:
        status, data = 400, {"error": f"{type(e).__name__}: {e}"}
    except Exception as e:
        status, data = 500, {"error": f"{type(e).__name__}: {e}"}

    data.setdefault("ms", round((time.perf_counter() - t0) * 1000, 1))
    return _json(status, data)
