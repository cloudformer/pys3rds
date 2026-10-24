"""pys3mysql 驱动:重试原则(宁可失败,绝不重复写)、DB-API 游标、%s 占位符。"""
import http.client
import json
import socket

import pytest

import pys3mysql
from pys3mysql import Error, _to_qmark


class FakeConn:
    """script 里每项对应一次 request():'ok' | 'disconnect' | 'timeout' | 'reset_read'。"""
    script = []
    sent = []  # 每次 request() 记一笔 → 服务端"收到"的次数
    reply = {"columns": [], "rows": [], "rowcount": 1, "lastrowid": 7, "ms": 1, "s3": "cache-hit"}

    def __init__(self, host, timeout=None):
        pass

    def request(self, method, path, body, headers):
        self.step = FakeConn.script.pop(0)
        if self.step == "timeout":
            raise socket.timeout("timed out")
        FakeConn.sent.append(json.loads(body))

    def getresponse(self):
        if self.step == "disconnect":
            raise http.client.RemoteDisconnected("closed")
        return self

    status = 200

    def read(self):
        if self.step == "reset_read":
            raise ConnectionResetError("reset while reading")
        return json.dumps(FakeConn.reply).encode()

    def close(self):
        pass


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConn)
    FakeConn.script = []
    FakeConn.sent = []
    return pys3mysql.connect(host="x.example", user="u", password="p", database="d",
                             port=3306, charset="utf8mb4")  # pymysql 的多余参数被忽略


# ---- 重试原则 ----

def test_stale_reused_connection_is_retried_once(conn):
    FakeConn.script = ["ok", "disconnect", "ok"]
    conn.execute("INSERT INTO t VALUES (1)")   # 建立连接
    conn.execute("INSERT INTO t VALUES (2)")   # 复用的连接被关 → 安全重发
    assert len(FakeConn.sent) == 3


def test_fresh_connection_failure_is_not_retried(conn):
    FakeConn.script = ["disconnect"]
    with pytest.raises(Error):
        conn.execute("INSERT INTO t VALUES (1)")
    assert len(FakeConn.sent) == 1


def test_timeout_is_never_retried(conn):
    FakeConn.script = ["ok", "timeout"]
    conn.execute("SELECT 1")
    with pytest.raises(Error, match="结果未知"):
        conn.execute("INSERT INTO t VALUES (1)")
    assert FakeConn.script == []  # 没有第三次尝试


def test_read_phase_failure_is_not_retried(conn):
    FakeConn.script = ["ok", "reset_read"]
    conn.execute("SELECT 1")
    with pytest.raises(Error, match="结果未知"):
        conn.execute("INSERT INTO t VALUES (1)")  # 服务端可能已执行,绝不重发
    assert len(FakeConn.sent) == 2


# ---- %s → ? ----

@pytest.mark.parametrize("src, expected", [
    ("SELECT * FROM t WHERE a = %s AND b = %s", "SELECT * FROM t WHERE a = ? AND b = ?"),
    ("SELECT * FROM t WHERE a = ?", "SELECT * FROM t WHERE a = ?"),
    ("SELECT '100%s' FROM t WHERE a = %s", "SELECT '100%s' FROM t WHERE a = ?"),  # 引号内不动
    ("SELECT \"%s\" FROM t", "SELECT \"%s\" FROM t"),
    ("SELECT 'it''s %s' FROM t WHERE a = %s", "SELECT 'it''s %s' FROM t WHERE a = ?"),
    ("SELECT a FROM t WHERE a LIKE '%%x%%' AND b = %s", "SELECT a FROM t WHERE a LIKE '%%x%%' AND b = ?"),
    ("SELECT 100 %% 7", "SELECT 100 % 7"),
])
def test_placeholder_translation(src, expected):
    assert _to_qmark(src) == expected


def test_execute_sends_qmark_sql(conn):
    FakeConn.script = ["ok"]
    conn.execute("INSERT INTO t (a) VALUES (%s)", ["x"])
    assert FakeConn.sent[0] == {"sql": "INSERT INTO t (a) VALUES (?)", "params": ["x"]}


# ---- DB-API 游标(pymysql 写法原样能跑) ----

def test_cursor_dml(conn):
    FakeConn.script = ["ok"]
    with conn.cursor() as cur:
        n = cur.execute("INSERT INTO t (a) VALUES (%s)", ["x"])
    assert n == 1 and cur.rowcount == 1 and cur.lastrowid == 7
    assert cur.description is None


def test_cursor_select_tuples_and_dicts(conn, monkeypatch):
    monkeypatch.setattr(FakeConn, "reply", {
        "columns": ["id", "a"], "rows": [[1, "x"], [2, "y"]], "rowcount": -1, "ms": 1, "s3": ""})
    FakeConn.script = ["ok", "ok", "ok"]
    with conn.cursor() as cur:
        cur.execute("SELECT id, a FROM t")
        assert [d[0] for d in cur.description] == ["id", "a"]
        assert cur.fetchone() == (1, "x")
        assert cur.fetchall() == [(2, "y")]
        assert cur.fetchone() is None
    with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, a FROM t")
        assert cur.fetchall() == [{"id": 1, "a": "x"}, {"id": 2, "a": "y"}]
    with conn.cursor() as cur:
        cur.execute("SELECT id, a FROM t")
        assert list(cur) == [(1, "x"), (2, "y")]


def test_executemany_is_one_transaction(conn, monkeypatch):
    captured = {}
    monkeypatch.setattr(conn, "_post", lambda path, payload: captured.update(
        path=path, payload=payload) or {"results": [], "ms": 1, "s3": "cache-hit+upload"})
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO t (a) VALUES (%s)", [["x"], ["y"]])
    assert captured["path"] == "/api/db/d/tx"
    assert captured["payload"]["statements"] == [
        {"sql": "INSERT INTO t (a) VALUES (?)", "params": ["x"]},
        {"sql": "INSERT INTO t (a) VALUES (?)", "params": ["y"]},
    ]


def test_commit_rollback_are_noops(conn):
    conn.commit()
    conn.rollback()
    assert FakeConn.sent == []
