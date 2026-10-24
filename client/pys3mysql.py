"""pys3mysql —— MySQL 驱动的接口形状,SQLite 引擎,S3 存储,Lambda 按需运行。

连接 srds(serverless RDS)的 Python 驱动。零依赖(只用标准库)。
接口对齐 pymysql:将来迁到真 RDS MySQL,把 `import pys3mysql` 换成 `import pymysql` 即可。

    import pys3mysql                                  # 将来: import pymysql
    conn = pys3mysql.connect(                         # 将来: pymysql.connect(
        host="xxx.lambda-url.us-east-2.on.aws",       #   host="xxx.rds.amazonaws.com",
        user="admin", password="...", database="blog")

    # DB-API 写法(和 pymysql 一样,占位符 %s)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO items (text) VALUES (%s)", ["hi"])
        new_id = cur.lastrowid
    with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM items WHERE id = %s", [new_id])
        rows = cur.fetchall()                        # [{"id": 1, "text": "hi"}]

    # 便捷写法(pys3mysql 独有,? 和 %s 都认)
    conn.execute("INSERT INTO items (text) VALUES (?)", ["hi"])
    rows = conn.query("SELECT * FROM items")          # [dict, ...]
    conn.transaction([("UPDATE ...", [..]), ("INSERT ...", [..])])  # 多语句一个事务
    conn.last                                         # {"ms": 服务端耗时, "s3": "cache-hit"...}

事务模型:每条 execute 自成事务并立即落盘(= pymysql 的 autocommit=True);
多条要同成同败用 transaction()。commit()/rollback() 为兼容保留,是空操作。

重试原则——宁可失败,绝不重复写:
  只有"复用的空闲连接被服务端关闭"(请求根本没送出去)才静默重连一次;
  超时等结果未知的情况一律抛 Error,由调用方决定怎么办。
(单个连接不是线程安全的,多线程各建各的。)
"""
import base64
import http.client
import json
import socket
import urllib.parse

__version__ = "0.1.0"

apilevel = "2.0"
threadsafety = 1
paramstyle = "format"  # %s,同 pymysql;? 也接受


# ---------------- 异常 ----------------

class Error(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status  # HTTP 状态码;0 = 网络层错误
        self.message = message


SrdsError = Error  # 旧名


class _NotDelivered(Exception):
    """请求确定没被服务端处理:发送阶段就失败,或连一个状态行都没收到。

    只有这种情况才允许重发;响应读取阶段的任何错误都不算——那时服务端可能已经执行了。
    """
    def __init__(self, cause, reused):
        super().__init__(str(cause))
        self.cause = cause
        self.reused = reused  # 是否发生在复用的长连接上(空闲连接被对端关闭的典型表现)


# ---------------- 占位符:%s(pymysql 风格)→ ?(SQLite) ----------------

def _to_qmark(sql):
    """把引号外的 %s 换成 ?,%% 换成 %;引号内原样。已经是 ? 风格的 SQL 原样返回。"""
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


# ---------------- 游标(DB-API 2.0 子集,够 pymysql 的常见用法) ----------------

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
        """同一条语句不同参数,整体一个事务(全成或全败)。"""
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


class cursors:  # 让 pys3mysql.cursors.DictCursor 这种 pymysql 式写法成立
    Cursor = Cursor
    DictCursor = DictCursor


# ---------------- 连接 ----------------

class Connection:
    def __init__(self, url, database, user="", password="", timeout=30, cursorclass=Cursor):
        p = urllib.parse.urlparse(url.rstrip("/"))
        self._host = p.netloc
        self._base_path = p.path
        self.database = database
        self.cursorclass = cursorclass
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.timeout = timeout
        self._conn = None  # 复用的 HTTPS 长连接
        # 性能感知:最近一次调用的服务端指标
        # {"ms": 服务端耗时毫秒, "s3": "cache-hit" | "download" | "...+upload"}
        self.last = {}

    # ---- 传输 ----

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
        # 阶段 1:发送。超时不算"没送达"(请求体可能已经完整到达);其他错误算
        try:
            self._conn.request("POST", self._base_path + path, body, headers)
        except (socket.timeout, TimeoutError):
            raise
        except (OSError, http.client.HTTPException) as e:
            raise _NotDelivered(e, reused) from None
        # 阶段 2:等状态行。一个字节都没收到 → 服务端没处理(Lambda URL 总会回状态)
        try:
            resp = self._conn.getresponse()
        except (http.client.RemoteDisconnected, http.client.BadStatusLine) as e:
            raise _NotDelivered(e, reused) from None
        # 阶段 3:读响应体。这里出任何错都不重试——服务端大概率已经执行
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
            if not e.reused:  # 新连接都连不上,不是"空闲连接被关",不猜
                raise Error(0, f"连接失败: {e.cause}") from None
            try:
                return self._send(path, body)  # 唯一的安全重发:请求确定没送达
            except _NotDelivered as e2:
                self._drop()
                raise Error(0, f"连接失败: {e2.cause}") from None
        except (socket.timeout, TimeoutError):
            self._drop()
            raise Error(0, "请求超时:结果未知(可能已执行),写操作请勿盲目重试") from None
        except (OSError, http.client.HTTPException) as e:
            self._drop()
            raise Error(0, f"网络错误(结果未知): {e}") from None

    # ---- DB-API ----

    def cursor(self, cursorclass=None):
        return (cursorclass or self.cursorclass)(self)

    def commit(self):
        """每条语句已经自动提交落盘;为 pymysql 兼容保留。"""

    def rollback(self):
        """没有跨语句的待提交状态可回滚;为 pymysql 兼容保留。多语句原子性用 transaction()。"""

    def close(self):
        self._drop()

    def ping(self, reconnect=True):
        self.execute("SELECT 1")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- 便捷方法 ----

    def create_database(self):
        """建库;已存在(409)时静默成功,所以可以在冷启动时无脑调。"""
        try:
            self._post("/api/databases", {"name": self.database})
        except Error as e:
            if e.status != 409:
                raise

    def execute(self, sql, params=()):
        """执行一条 SQL(? 或 %s 占位符),返回 {columns, rows, rowcount, lastrowid, truncated, ms, s3}。"""
        r = self._post(f"/api/db/{self.database}/query",
                       {"sql": _to_qmark(sql), "params": list(params)})
        self.last = {"ms": r.get("ms"), "s3": r.get("s3")}
        return r

    def query(self, sql, params=()):
        """SELECT 便捷方法,返回 [dict, ...]。超过 500 行会被截断(看 execute 的 truncated)。"""
        r = self.execute(sql, params)
        return [dict(zip(r["columns"], row)) for row in r["rows"]]

    def transaction(self, statements):
        """多条语句在一个事务里执行:全部成功才提交落盘,任一失败整体回滚、S3 不动。

        statements 每项是 "sql" 或 (sql, params)。返回每条语句的结果列表。
        """
        stmts = [{"sql": _to_qmark(s), "params": []} if isinstance(s, str)
                 else {"sql": _to_qmark(s[0]), "params": list(s[1]) if len(s) > 1 else []}
                 for s in statements]
        r = self._post(f"/api/db/{self.database}/tx", {"statements": stmts})
        self.last = {"ms": r.get("ms"), "s3": r.get("s3")}
        return r["results"]


SrdsClient = Connection  # 旧名


# ---------------- 模块入口,签名同 pymysql.connect ----------------

def connect(host, user="", password="", database="", timeout=30,
            cursorclass=Cursor, **_ignored):
    """多余的 pymysql 参数(port、charset、autocommit…)接受并忽略,方便原样替换。

    host 可以带或不带 https://(Lambda Function URL 只有 https)。
    """
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, database, user, password, timeout, cursorclass)
