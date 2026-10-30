"""s3rds.sqlite end-to-end against the real server code (in-process, fake S3)."""
import base64
import datetime
import decimal
import json

import pytest

from s3rds.server import lambda_function as lf
from s3rds import sqlite

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()


@pytest.fixture
def conn(fake_s3, monkeypatch, bucket):
    """An s3rds.sqlite connection whose HTTP layer calls the lambda handler in-process."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    c = sqlite.connect(host="in-process.test", user="admin", password="pw", database="d")

    def post(path, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                 "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
        resp = lf.lambda_handler(event, None)
        data = json.loads(resp["body"])
        if resp["statusCode"] >= 400:
            raise sqlite.Error(resp["statusCode"], data.get("error", ""))
        return data

    monkeypatch.setattr(c, "_post", post)
    c.create_database()
    return c


# ---- task 3: no silent truncation; paging is transparent ----

def test_1200_rows_come_back_complete(conn):
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT) STRICT")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO t (v) VALUES (?)", [[f"row-{i}"] for i in range(1200)])
        cur.execute("SELECT * FROM t ORDER BY id")
        rows = cur.fetchall()
    assert len(rows) == 1200 and rows[-1] == (1200, "row-1199")


def test_result_over_6mb_is_paged_and_complete(conn):
    conn.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, blob TEXT) STRICT")
    chunk = "x" * 500_000  # 14 rows x 0.5 MB = 7 MB > the 6 MB Lambda limit, > one 4 MB page
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO big (blob) VALUES (?)", [[chunk]] * 14)
    rows = conn.query("SELECT id, blob FROM big ORDER BY id")
    assert [r["id"] for r in rows] == list(range(1, 15))
    assert all(len(r["blob"]) == 500_000 for r in rows)


def test_version_change_while_paging_is_retried(conn, monkeypatch):
    monkeypatch.setattr(lf, "PAGE_BYTES", 2000)  # many small pages without big data
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT) STRICT")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO t (v) VALUES (?)", [[f"row-{i:04d}"] for i in range(300)])

    real_post, calls = conn._post, {"pages": 0, "intruded": False}

    def post(path, payload):
        if "next_token" in payload:
            calls["pages"] += 1
            if not calls["intruded"]:  # somebody writes between page 1 and page 2
                calls["intruded"] = True
                real_post(path, {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["late"]})
        return real_post(path, payload)

    monkeypatch.setattr(conn, "_post", post)
    rows = conn.query("SELECT v FROM t ORDER BY id")
    assert len(rows) == 301 and rows[-1] == {"v": "late"}  # re-ran from page 1 on the new version
    assert calls["pages"] > 2


def test_old_server_truncation_is_an_error(conn, monkeypatch):
    monkeypatch.setattr(conn, "_post", lambda path, payload: {
        "columns": ["a"], "rows": [[1]], "rowcount": -1, "truncated": True, "ms": 1, "s3": "cache-hit"})
    with pytest.raises(sqlite.Error, match="truncated"):
        conn.query("SELECT a FROM t")


def test_tx_refuses_oversized_result(conn, monkeypatch):
    monkeypatch.setattr(lf, "PAGE_BYTES", 100)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT) STRICT")
    conn.transaction([("INSERT INTO t (v) VALUES (?)", ["x" * 50])] * 5)
    with pytest.raises(sqlite.Error, match="exceeds"):
        conn.transaction(["SELECT * FROM t"])


# ---- task 4: rollback() warns when something was written, stays quiet otherwise ----

def test_rollback_warns_only_after_writes(conn):
    import warnings
    conn.execute("CREATE TABLE t (v INTEGER) STRICT")
    conn.commit()
    with warnings.catch_warnings():
        warnings.simplefilter("error")        # any warning would fail the test
        conn.query("SELECT 1")
        conn.rollback()                       # nothing written since commit: silent
    conn.execute("INSERT INTO t (v) VALUES (1)")
    with pytest.warns(RuntimeWarning, match="cannot undo"):
        conn.rollback()
    assert conn.query("SELECT v FROM t") == [{"v": 1}]  # the insert is still there, as documented
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        conn.rollback()                       # flag was cleared by the previous rollback


# ---- task 2: executemany rowcount / lastrowid ----

def test_executemany_counts_affected_rows(conn):
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, grp TEXT, v INTEGER) STRICT")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO t (grp, v) VALUES (?, ?)", [["a", 1], ["a", 2], ["b", 3]])
        assert cur.rowcount == 3 and cur.lastrowid == 3
        cur.executemany("UPDATE t SET v = v + 10 WHERE grp = ?", [["a"], ["b"], ["none"]])
        assert cur.rowcount == 3  # 2 + 1 + 0, not the 3 parameter sets
        cur.executemany("DELETE FROM t WHERE grp = ?", [["a"]])
        assert cur.rowcount == 2
    assert conn.query("SELECT v FROM t") == [{"v": 13}]


# ---- task 1: parameter encoding ----

@pytest.mark.parametrize("value, stored", [
    (datetime.datetime(2026, 1, 2, 3, 4, 5), "2026-01-02 03:04:05"),
    (datetime.datetime(2026, 1, 2, 3, 4, 5, 123456), "2026-01-02 03:04:05.123456"),
    (datetime.date(2026, 1, 2), "2026-01-02"),
    (datetime.time(3, 4, 5), "03:04:05"),
    (decimal.Decimal("12345678901234567890.123456789"), "12345678901234567890.123456789"),
    (True, 1),
    (False, 0),
    (None, None),
    (42, 42),
    (1.5, 1.5),
    ("text", "text"),
])
def test_parameter_round_trip(conn, value, stored):
    conn.execute("CREATE TABLE t (v ANY) STRICT")
    conn.execute("INSERT INTO t (v) VALUES (?)", [value])
    assert conn.query("SELECT v FROM t") == [{"v": stored}]


def test_unencodable_parameters_raise_s3rds_error(conn):
    conn.execute("CREATE TABLE t (v ANY) STRICT")
    conn.execute("INSERT INTO t (v) VALUES (?)", [b"\x00\x01"])            # bytes travel as {"$b": base64}
    assert conn.query("SELECT v FROM t") == [{"v": b"\x00\x01"}]
    conn.execute("DELETE FROM t")

    class Thing:
        pass

    with pytest.raises(sqlite.Error, match=r"parameter #2: type Thing"):
        conn.execute("INSERT INTO t (v) VALUES (?), (?)", [1, Thing()])
    assert conn.query("SELECT COUNT(*) AS n FROM t") == [{"n": 0}]  # nothing was sent


def test_transaction_parameters_are_encoded(conn):
    conn.execute("CREATE TABLE t (v ANY) STRICT")
    conn.transaction([("INSERT INTO t (v) VALUES (?)", [datetime.date(2026, 1, 2)]),
                      ("INSERT INTO t (v) VALUES (?)", [decimal.Decimal("1.10")])])
    assert conn.query("SELECT v FROM t ORDER BY rowid") == [{"v": "2026-01-02"}, {"v": "1.10"}]
