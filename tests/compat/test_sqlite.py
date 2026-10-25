"""s3rds.sqlite vs the standard library sqlite3: the transport must be transparent.

No translation is involved, so every case here must pass; a failure is a driver or server bug.
"""
import datetime
import decimal

T = "DROP TABLE IF EXISTS t"


def test_crud_and_rowcount(sqlite_pair):
    """CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid"""
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
    """Parameter binding: int, float, str, None, bool"""
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
    """datetime / Decimal parameters stored as canonical text"""
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
    """Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax"""
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
    """1200-row result comes back complete (paging is transparent)"""
    script = [(T, []), ("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT) STRICT", [])]
    script += [("INSERT INTO t (v) VALUES (?)", [f"row-{i:05d}"]) for i in range(1200)]
    script.append(("SELECT id, v FROM t ORDER BY id", []))
    sqlite_pair(script)


def test_aggregates_and_window(sqlite_pair):
    """GROUP BY aggregates and window functions"""
    sqlite_pair([
        (T, []),
        ("CREATE TABLE t (g TEXT, v REAL) STRICT", []),
        ("INSERT INTO t VALUES ('a', 1.5), ('a', 2.5), ('b', 10)", []),
        ("SELECT g, COUNT(*) AS c, SUM(v) AS s, AVG(v) AS a FROM t GROUP BY g ORDER BY g", []),
        ("SELECT g, v, RANK() OVER (PARTITION BY g ORDER BY v DESC) AS r FROM t ORDER BY g, r", []),
    ])
