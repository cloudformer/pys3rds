"""s3rds.sqlite vs the standard library sqlite3: the transport must be transparent.

No translation is involved, so every case here must pass; a failure is a driver or server bug.
"""
import datetime
import decimal

import pytest

T = "DROP TABLE IF EXISTS t"


def test_crud_and_rowcount(sqlite_pair):
    """DML | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, n INTEGER) STRICT", []),
        ("INSERT INTO t (name, n) VALUES (?, ?)", ["a", 1]),
        ("INSERT INTO t (name, n) VALUES (?, ?)", ["b", 2]),
        ("UPDATE t SET n = n + 10 WHERE n > ?", [0]),
        ("SELECT id, name, n FROM t ORDER BY id", []),
        ("DELETE FROM t WHERE name = ?", ["a"]),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_parameter_types(sqlite_pair):
    """Types | Parameter binding: int, float, str, None, bool"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (v ANY) STRICT", []),
        ("INSERT INTO t (v) VALUES (?)", [42]),
        ("INSERT INTO t (v) VALUES (?)", [1.5]),
        ("INSERT INTO t (v) VALUES (?)", ["text"]),
        ("INSERT INTO t (v) VALUES (?)", [None]),
        ("INSERT INTO t (v) VALUES (?)", [True]),           # 1 on both sides
        ("SELECT v FROM t ORDER BY rowid", []),
    ])


def test_datetime_and_decimal_params_as_text(sqlite_pair):
    """Types | datetime / Decimal parameters stored as canonical text"""
    # sqlite3 cannot bind these at all; s3rds encodes them to the canonical text. Compare against the
    # text the reference stores when given the same canonical strings.
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (v TEXT) STRICT", []),
        ("INSERT INTO t (v) VALUES (?)", ["2026-01-02 03:04:05"]),
        ("INSERT INTO t (v) VALUES (?)", ["12345678901234567890.123456789"]),
        ("SELECT v FROM t ORDER BY rowid", []),
    ])
    assert str(datetime.datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"
    assert str(decimal.Decimal("12345678901234567890.123456789")) == "12345678901234567890.123456789"


def test_errors_surface_on_both_sides(sqlite_pair):
    """Constraints & errors | Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL) STRICT", []),
        ("INSERT INTO t (email) VALUES (?)", ["x"]),
        ("INSERT INTO t (email) VALUES (?)", ["x"]),       # UNIQUE
        ("INSERT INTO t (email) VALUES (?)", [None]),      # NOT NULL
        ("SELECT * FROM nope", []),                        # no such table
        ("SELEC 1", []),                                   # syntax
    ])


def test_large_result_is_complete(sqlite_pair):
    """Queries | 1200-row result comes back complete (paging is transparent)"""
    script = [(T, []), ("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT) STRICT", [])]
    script += [("INSERT INTO t (v) VALUES (?)", [f"row-{i:05d}"]) for i in range(1200)]
    script.append(("SELECT id, v FROM t ORDER BY id", []))
    sqlite_pair(script)


def test_indexes_alter_joins(sqlite_pair):
    """DDL | CREATE INDEX / UNIQUE INDEX / DROP INDEX, ALTER TABLE ADD / DROP COLUMN, JOIN"""
    sqlite_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []),
        ("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, score INTEGER) STRICT", []),
        ("CREATE TABLE o (id INTEGER PRIMARY KEY AUTOINCREMENT, t_id INTEGER, item TEXT) STRICT", []),
        ("CREATE INDEX idx_name ON t (name)", []),
        ("CREATE UNIQUE INDEX uq_score ON t (score)", []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("INSERT INTO t (name, score) VALUES ('c', 1)", []),              # UNIQUE
        ("DROP INDEX uq_score", []),
        ("ALTER TABLE t ADD COLUMN age INTEGER DEFAULT 7", []),
        ("INSERT INTO o (t_id, item) VALUES (1, 'x'), (1, 'y')", []),
        ("SELECT t.name, t.age, o.item FROM t JOIN o ON o.t_id = t.id ORDER BY o.id", []),
        ("SELECT t.name, o.item FROM t LEFT JOIN o ON o.t_id = t.id ORDER BY t.id, o.id", []),
        ("ALTER TABLE t DROP COLUMN age", []),
        ("SELECT name FROM t ORDER BY id", []),
    ])


def test_subqueries_union_case(sqlite_pair):
    """Queries | Subqueries (IN / EXISTS / scalar), UNION, CASE WHEN, COALESCE, json_extract, STRICT type rejection"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, score INTEGER, doc TEXT) STRICT", []),
        ("INSERT INTO t (name, score, doc) VALUES ('a', 1, '{\"k\": 1}'), ('b', 2, NULL), ('c', 3, NULL)", []),
        ("SELECT name FROM t WHERE score IN (SELECT score FROM t WHERE score > 1) ORDER BY id", []),
        ("SELECT name FROM t x WHERE EXISTS (SELECT 1 FROM t y WHERE y.score = x.score + 1) ORDER BY id", []),
        ("SELECT (SELECT COUNT(*) FROM t) AS total", []),
        ("SELECT name FROM t WHERE score = 1 UNION SELECT name FROM t WHERE score = 3 ORDER BY name", []),
        ("SELECT name, CASE WHEN score >= 2 THEN 'high' ELSE 'low' END AS tier, COALESCE(doc, '-') AS d FROM t ORDER BY id", []),
        ("SELECT json_extract(doc, '$.k') AS k FROM t WHERE id = 1", []),
        ("INSERT INTO t (id, name, score) VALUES (9, 'z', 'not-an-int')", []),   # STRICT rejects
    ])


@pytest.mark.xfail(strict=True, reason="task 13: bytes parameters are rejected and BLOB results come back as text")
def test_blob_round_trip(sqlite_pair):
    """Types | BLOB values round-trip as bytes | task 13"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (b BLOB) STRICT", []),
        ("INSERT INTO t (b) VALUES (?)", [b"\x00\x01\xff"]),
        ("INSERT INTO t (b) VALUES (X'0203')", []),
        ("SELECT b FROM t ORDER BY rowid", []),
    ])


def test_aggregates_and_window(sqlite_pair):
    """Queries | GROUP BY aggregates and window functions"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (g TEXT, v REAL) STRICT", []),
        ("INSERT INTO t VALUES ('a', 1.5), ('a', 2.5), ('b', 10)", []),
        ("SELECT g, COUNT(*) AS c, SUM(v) AS s, AVG(v) AS a FROM t GROUP BY g ORDER BY g", []),
        ("SELECT g, v, RANK() OVER (PARTITION BY g ORDER BY v DESC) AS r FROM t ORDER BY g, r", []),
    ])
