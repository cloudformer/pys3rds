"""s3rds.pymysql vs a real MySQL 8.0 (Docker): the whitelist's evidence.

Every test is one feature; its docstring is the feature name in the report. A test without a marker
is verified and may be listed as supported. A test marked xfail(strict=True) is a known gap: the
reason names the task that will close it, and it turns into a hard failure the moment the fix lands
so the marker gets removed. Run with:

    docker compose -f tests/compat/docker-compose.yml up -d mysql
    MYSQL_DSN=mysql://root:test@127.0.0.1:3306/compat python3 -m pytest tests/compat/test_mysql.py -v

The report is written to tests/compat/REPORT.md.
"""
import pytest

T = "DROP TABLE IF EXISTS t"
USERS = ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL, "
         "email VARCHAR(100) UNIQUE, score INT DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")

# ---- verified ----

def test_crud(mysql_pair):
    """CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, email) VALUES (%s, %s)", ["alice", "a@x"]),
        ("INSERT INTO t (name, email) VALUES (%(n)s, %(e)s)", {"n": "bob", "e": "b@x"}),
        ("UPDATE t SET score = score + %s WHERE name = %s", [5, "alice"]),
        ("SELECT id, name, email, score FROM `t` ORDER BY id", []),
        ("DELETE FROM t WHERE name = %s", ["bob"]),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_case_insensitive_compare(mysql_pair):
    """Case-insensitive string comparison (MySQL default collation)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('alice')", []),
        ("SELECT name FROM t WHERE name = 'ALICE'", []),
    ])


def test_limit_offset(mysql_pair):
    """LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b'), ('c')", []),
        ("SELECT name FROM t ORDER BY id LIMIT 1, 1", []),
        ("SELECT name FROM t ORDER BY id LIMIT 2 OFFSET 1", []),
    ])


def test_upsert(mysql_pair):
    """INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name, email) VALUES (1, 'a', 'a@x')", []),
        ("INSERT IGNORE INTO t (id, name, email) VALUES (1, 'zzz', 'a@x')", []),
        # when ON DUPLICATE KEY updates an existing row MySQL reports rowcount 2 and lastrowid 0 (documented quirks)
        ("INSERT INTO t (id, name, email) VALUES (1, 'a2', 'a@x') ON DUPLICATE KEY UPDATE name = VALUES(name)", [],
         {"ignore": "rowcount,lastrowid"}),
        ("SELECT id, name FROM t", []),
    ])


def test_error_codes(mysql_pair):
    """Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, email) VALUES ('a', 'dup')", []),
        ("INSERT INTO t (name, email) VALUES ('b', 'dup')", []),   # 1062
        ("SELECT * FROM nope", []),                                  # 1146
        ("SELECT nope FROM t", []),                                  # 1054
        ("SELEC 1", []),                                             # 1064
        ("INSERT INTO t (name, email) VALUES (NULL, 'c')", []),      # 1048 explicit NULL
    ])


def test_string_functions(mysql_pair):
    """String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING"""
    mysql_pair([
        ("SELECT IFNULL(NULL, 'd') AS v, CONCAT_WS('-', 'a', 'b') AS w, CONCAT('x', 'y') AS c, "
         "UPPER('ab') AS u, LENGTH('abc') AS l, SUBSTRING('hello', 2, 3) AS s", []),
    ])


def test_last_insert_id(mysql_pair):
    """LAST_INSERT_ID() after an INSERT"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("SELECT LAST_INSERT_ID() AS id", []),
    ])


def test_show_tables(mysql_pair):
    """SHOW TABLES"""
    mysql_pair([(T, []), (USERS, [])])
    real, ours = mysql_pair.both("SHOW TABLES")
    key = "Tables_in_compat"
    assert key in real.rows[0] and key in ours.rows[0]
    assert "t" in {r[key] for r in real.rows} and "t" in {r[key] for r in ours.rows}  # other tables may exist on the real side


# ---- known gaps: each marker names the task in the MySQL spec that closes it ----

@pytest.mark.xfail(strict=True, reason="task 8: DATETIME/DECIMAL come back as str/int, pymysql returns datetime/Decimal")
def test_result_types(mysql_pair):
    """Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (d DATETIME, x DECIMAL(10,2), b TINYINT(1))", []),
        ("INSERT INTO t VALUES ('2026-01-02 03:04:05', 1.25, 1)", []),
        ("SELECT d, x, b FROM t", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 8: SUM() is Decimal in MySQL, int here")
def test_aggregate_types(mysql_pair):
    """Aggregate result types: COUNT(*) int, SUM(INT) Decimal"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("SELECT COUNT(*) AS c, SUM(score) AS s FROM t", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 6: DATE_ADD drops the time part (DATE() instead of datetime())")
def test_date_add_keeps_time(mysql_pair):
    """DATE_ADD / DATE_SUB with INTERVAL keep the time part"""
    mysql_pair([
        ("SELECT DATE_ADD('2026-01-01 10:30:00', INTERVAL 1 DAY) AS d", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 7: inline KEY idx (col) is dropped instead of becoming CREATE INDEX")
def test_inline_index_exists(mysql_pair):
    """Inline KEY / INDEX in CREATE TABLE creates the index"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(100), KEY idx_title (title))", []),
        ("SELECT COUNT(*) AS n FROM information_schema.statistics WHERE table_name = 't' AND index_name = 'idx_title'", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 9: NOW() returns datetime in pymysql, str here")
def test_now_type(mysql_pair):
    """NOW() / CURDATE() return datetime / date objects"""
    mysql_pair([("SELECT NOW() IS NOT NULL AS ok, DATE(NOW()) = CURDATE() AS same, NOW() AS n", [])])


@pytest.mark.xfail(strict=True, reason="task 5: invalid ENUM value is 1265 in MySQL strict mode, 3819 (CHECK) here")
def test_enum_error_code(mysql_pair):
    """ENUM rejects values outside the list with MySQL's error code"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (s ENUM('a','b'))", []),
        ("INSERT INTO t (s) VALUES ('zzz')", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 5: omitted NOT NULL column is 1364 in MySQL strict mode, 1048 here (SQLite cannot tell the two apart)")
def test_missing_column_error_code(mysql_pair):
    """Omitting a NOT NULL column without default raises 1364"""
    mysql_pair([(T, []), (USERS, []), ("INSERT INTO t (email) VALUES ('c')", [])])


@pytest.mark.xfail(strict=True, reason="task 5: for an upsert that updated an existing row MySQL reports rowcount 2 / lastrowid 0")
def test_upsert_rowcount_quirk(mysql_pair):
    """ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name) VALUES (1, 'a')", []),
        ("INSERT INTO t (id, name) VALUES (1, 'b') ON DUPLICATE KEY UPDATE name = VALUES(name)", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 12: DESCRIBE shows SQLite type names (text(50)) not MySQL's (varchar(50))")
def test_describe(mysql_pair):
    """DESCRIBE shows MySQL type names"""
    mysql_pair([
        (T, []), (USERS, []),
        ("DESCRIBE t", []),
    ])
