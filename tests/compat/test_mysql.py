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

# The production engine is the pinned one (extra "server"), the same module this suite runs
from s3rds.server.s3_sqlite import sqlite3 as ENGINE  # noqa: E402

LAMBDA_SQLITE = ENGINE.sqlite_version_info
LAMBDA_UPDATE_DELETE_LIMIT = bool(ENGINE.connect(":memory:").execute(
    "SELECT sqlite_compileoption_used('ENABLE_UPDATE_DELETE_LIMIT')").fetchone()[0])

T = "DROP TABLE IF EXISTS t"
USERS = ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL, "
         "email VARCHAR(100) UNIQUE, score INT DEFAULT 0) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")

# ---- verified ----

def test_crud(mysql_pair):
    """DML | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders"""
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
    """Queries | Case-insensitive string comparison (MySQL default collation)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('alice')", []),
        ("SELECT name FROM t WHERE name = 'ALICE'", []),
    ])


def test_limit_offset(mysql_pair):
    """Queries | LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b'), ('c')", []),
        ("SELECT name FROM t ORDER BY id LIMIT 1, 1", []),
        ("SELECT name FROM t ORDER BY id LIMIT 2 OFFSET 1", []),
    ])


def test_insert_ignore(mysql_pair):
    """DML | INSERT IGNORE"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name, email) VALUES (1, 'a', 'a@x')", []),
        ("INSERT IGNORE INTO t (id, name, email) VALUES (1, 'zzz', 'a@x')", []),
        ("SELECT id, name FROM t", []),
    ])


def test_upsert(mysql_pair):
    """DML | INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name, email) VALUES (1, 'a', 'a@x')", []),
        # when ON DUPLICATE KEY updates an existing row MySQL reports rowcount 2 and lastrowid 0 (documented quirks)
        ("INSERT INTO t (id, name, email) VALUES (1, 'a2', 'a@x') ON DUPLICATE KEY UPDATE name = VALUES(name)", [],
         {"ignore": "rowcount,lastrowid"}),
        ("SELECT id, name FROM t", []),
    ])


def test_error_codes(mysql_pair):
    """Constraints & errors | Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL"""
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
    """Functions | String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING"""
    mysql_pair([
        ("SELECT IFNULL(NULL, 'd') AS v, CONCAT_WS('-', 'a', 'b') AS w, CONCAT('x', 'y') AS c, "
         "UPPER('ab') AS u, LENGTH('abc') AS l, SUBSTRING('hello', 2, 3) AS s", []),
        ("SELECT CONCAT_WS('-', 'a', NULL, 'b') AS a, CONCAT_WS(',', NULL) AS b, CONCAT_WS(NULL, 'x') AS c, "
         "CONCAT_WS(', ', 1, 2.5) AS d", []),
    ])


def test_last_insert_id(mysql_pair):
    """Functions | LAST_INSERT_ID() after an INSERT"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("SELECT LAST_INSERT_ID() AS id", []),
    ])


def test_show_tables(mysql_pair):
    """Metadata | SHOW TABLES"""
    mysql_pair([(T, []), (USERS, [])])
    real, ours = mysql_pair.both("SHOW TABLES")
    key = "Tables_in_compat"
    assert key in real.rows[0] and key in ours.rows[0]
    assert "t" in {r[key] for r in real.rows} and "t" in {r[key] for r in ours.rows}  # other tables may exist on the real side


def test_create_table_types(mysql_pair):
    """DDL | CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, n BIGINT NOT NULL DEFAULT 0, "
         "s VARCHAR(20) DEFAULT 'x', body TEXT, flag BOOLEAN DEFAULT TRUE)", []),
        ("INSERT INTO t (body) VALUES ('hello')", []),
        ("SELECT id, n, s, body, flag FROM t", []),
        ("DROP TABLE t", []),
    ])


def test_create_and_drop_index(mysql_pair):
    """DDL | CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("CREATE INDEX idx_name ON t (name)", []),
        ("CREATE UNIQUE INDEX uq_score ON t (score)", []),
        ("INSERT INTO t (name, score) VALUES ('a', 1)", []),
        ("INSERT INTO t (name, score) VALUES ('b', 1)", []),     # 1062 via the unique index
        ("DROP INDEX uq_score ON t", []),
        # allowed now; lastrowid differs because InnoDB burns an AUTO_INCREMENT value on the failed insert above
        ("INSERT INTO t (name, score) VALUES ('b', 1)", [], {"ignore": "lastrowid"}),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_alter_table_add_drop_column(mysql_pair):
    """DDL | ALTER TABLE ADD COLUMN / DROP COLUMN"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("ALTER TABLE t ADD COLUMN age INT DEFAULT 7", []),
        ("SELECT name, age FROM t", []),
        ("ALTER TABLE t DROP COLUMN age", []),
        ("SELECT name FROM t", []),
    ])


def test_where_operators(mysql_pair):
    """Queries | WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, email, score) VALUES ('alice', 'a@x', 10), ('bob', NULL, 20), ('carol', 'c@x', 30)", []),
        ("SELECT name FROM t WHERE name LIKE 'a%' ORDER BY id", []),
        ("SELECT name FROM t WHERE name IN ('bob', 'carol') ORDER BY id", []),
        ("SELECT name FROM t WHERE score BETWEEN 15 AND 25", []),
        ("SELECT name FROM t WHERE email IS NULL", []),
        ("SELECT name FROM t WHERE score <> 20 AND (name = 'alice' OR score > 25) ORDER BY id", []),
    ])


def test_order_distinct_group_having(mysql_pair):
    """Queries | ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('a', 2), ('b', 3), ('c', 3)", []),
        ("SELECT DISTINCT score FROM t ORDER BY score DESC", []),
        ("SELECT name, COUNT(*) AS c FROM t GROUP BY name HAVING COUNT(*) > 1 ORDER BY name", []),
        ("SELECT name FROM t ORDER BY score DESC, name ASC", []),
    ])


def test_joins(mysql_pair):
    """Queries | INNER JOIN and LEFT JOIN"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("CREATE TABLE o (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, item VARCHAR(20))", []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("INSERT INTO o (user_id, item) VALUES (1, 'x'), (1, 'y')", []),
        ("SELECT t.name, o.item FROM t INNER JOIN o ON o.user_id = t.id ORDER BY o.id", []),
        ("SELECT t.name, o.item FROM t LEFT JOIN o ON o.user_id = t.id ORDER BY t.id, o.id", []),
        ("DROP TABLE o", []),
    ])


def test_subqueries_and_union(mysql_pair):
    """Queries | Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2), ('c', 3)", []),
        ("SELECT name FROM t WHERE score IN (SELECT score FROM t WHERE score > 1) ORDER BY id", []),
        ("SELECT name FROM t x WHERE EXISTS (SELECT 1 FROM t y WHERE y.score = x.score + 1) ORDER BY id", []),
        ("SELECT (SELECT COUNT(*) FROM t) AS total", []),
        ("SELECT name FROM t WHERE score = 1 UNION SELECT name FROM t WHERE score = 3 ORDER BY name", []),
        ("SELECT 1 AS v UNION ALL SELECT 1 AS v", []),
    ])


def test_case_and_null_functions(mysql_pair):
    """Functions | CASE WHEN, COALESCE, NULLIF"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, email, score) VALUES ('a', NULL, 5), ('b', 'b@x', 50)", []),
        ("SELECT name, CASE WHEN score >= 10 THEN 'high' ELSE 'low' END AS tier FROM t ORDER BY id", []),
        ("SELECT COALESCE(email, 'none') AS e, NULLIF(score, 5) AS s FROM t ORDER BY id", []),
    ])


def test_math_and_more_string_functions(mysql_pair):
    """Functions | ABS, MOD, LOWER, TRIM, REPLACE, LEFT, RIGHT, INSTR"""
    mysql_pair([
        ("SELECT ABS(-3) AS a, MOD(7, 3) AS m, LOWER('AB') AS l, TRIM('  x  ') AS t, "
         "REPLACE('hello', 'l', 'L') AS r, LEFT('hello', 2) AS le, RIGHT('hello', 2) AS ri, INSTR('hello', 'l') AS i", []),
    ])


def test_foreign_key(mysql_pair):
    """Constraints & errors | FOREIGN KEY enforced on insert (1452)"""
    mysql_pair([
        ("DROP TABLE IF EXISTS c", []), (T, []), (USERS, []),
        ("CREATE TABLE c (id INT AUTO_INCREMENT PRIMARY KEY, t_id INT, FOREIGN KEY (t_id) REFERENCES t(id))", []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("INSERT INTO c (t_id) VALUES (1)", []),
        ("INSERT INTO c (t_id) VALUES (999)", []),     # 1452
        ("DROP TABLE c", []),
    ])


def test_replace_into(mysql_pair):
    """DML | REPLACE INTO"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name) VALUES (1, 'a')", []),
        ("REPLACE INTO t (id, name) VALUES (1, 'b')", [], {"ignore": "rowcount,lastrowid"}),  # MySQL counts delete+insert
        ("SELECT id, name FROM t", []),
    ])


def test_auto_increment_does_not_reuse_ids(mysql_pair):
    """DDL | AUTO_INCREMENT never reuses a deleted id"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("DELETE FROM t WHERE id = 2", []),
        ("INSERT INTO t (name) VALUES ('c')", []),
        ("SELECT id, name FROM t ORDER BY id", []),
    ])


def test_truncate(mysql_pair):
    """DML | TRUNCATE TABLE"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("TRUNCATE TABLE t", [], {"ignore": "rowcount"}),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_if_exists(mysql_pair):
    """DDL | CREATE TABLE IF NOT EXISTS / DROP TABLE IF EXISTS"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE IF NOT EXISTS t (id INT PRIMARY KEY)", []),
        ("CREATE TABLE IF NOT EXISTS t (id INT PRIMARY KEY)", []),
        ("DROP TABLE IF EXISTS nope", []),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_table_and_column_options(mysql_pair):
    """DDL | ENGINE / CHARSET / COLLATE / COMMENT / UNSIGNED options accepted"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY, "
         "name VARCHAR(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT 'the name', "
         "n INT UNSIGNED DEFAULT 0 COMMENT 'count') ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='users'", []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("SELECT id, name, n FROM t", []),
    ])


def test_composite_primary_key(mysql_pair):
    """DDL | Composite PRIMARY KEY (a, b)"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (a INT, b INT, v INT, PRIMARY KEY (a, b))", []),
        ("INSERT INTO t VALUES (1, 1, 0), (1, 2, 0)", []),
        ("INSERT INTO t VALUES (1, 1, 9)", []),                 # 1062
        ("SELECT a, b, v FROM t ORDER BY a, b", []),
    ])


def test_unique_constraints(mysql_pair):
    """DDL | UNIQUE column constraint and UNIQUE KEY (a, b) table constraint"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, a INT, b INT UNIQUE, UNIQUE KEY uq_ab (a, b))", []),
        ("INSERT INTO t (a, b) VALUES (1, 1), (1, 2)", []),
        ("INSERT INTO t (a, b) VALUES (1, 1)", []),             # 1062 on uq_ab
        ("INSERT INTO t (a, b) VALUES (2, 1)", []),             # 1062 on b
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_foreign_key_cascade(mysql_pair):
    """DDL | FOREIGN KEY ... ON DELETE CASCADE"""
    mysql_pair([
        ("DROP TABLE IF EXISTS c", []), (T, []), (USERS, []),
        ("CREATE TABLE c (id INT AUTO_INCREMENT PRIMARY KEY, t_id INT, FOREIGN KEY (t_id) REFERENCES t(id) ON DELETE CASCADE)", []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("INSERT INTO c (t_id) VALUES (1), (1), (2)", []),
        ("DELETE FROM t WHERE id = 1", []),
        ("SELECT COUNT(*) AS c FROM c", []),
        ("DROP TABLE c", []),
    ])


def test_enum_column(mysql_pair):
    """DDL | ENUM column accepts listed values and its DEFAULT"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, s ENUM('a','b') NOT NULL DEFAULT 'a')", []),
        ("INSERT INTO t (s) VALUES ('b')", []),
        ("INSERT INTO t (id) VALUES (NULL)", []),
        ("SELECT id, s FROM t ORDER BY id", []),
    ])


def test_decimal_column(mysql_pair):
    """DDL | DECIMAL(p, s) column keeps its scale and compares exactly"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (x DECIMAL(10,2))", []),
        ("INSERT INTO t VALUES (1.10), (2.20), (0.1)", []),
        ("SELECT COUNT(*) AS c FROM t WHERE x = 1.10", []),
        ("SELECT x FROM t ORDER BY x", []),
    ])


def test_datetime_columns(mysql_pair):
    """DDL | DATE / DATETIME / TIME columns store and compare"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (d DATE, dt DATETIME, tm TIME)", []),
        ("INSERT INTO t VALUES ('2026-01-02', '2026-01-02 03:04:05', '03:04:05')", []),
        ("SELECT d = '2026-01-02' AS d_ok, dt = '2026-01-02 03:04:05' AS dt_ok, tm = '03:04:05' AS tm_ok, "
         "dt > '2026-01-01' AS gt FROM t", []),
    ])


def test_timestamp_default(mysql_pair):
    """DDL | DATETIME / TIMESTAMP DEFAULT CURRENT_TIMESTAMP (ON UPDATE accepted)"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, c TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
         "u DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)", []),
        ("INSERT INTO t (id) VALUES (NULL)", []),
        ("SELECT c >= '2026-01-01' AS c_ok, u >= '2026-01-01' AS u_ok FROM t", []),
    ])


def test_json_column(mysql_pair):
    """DDL | JSON column stores a document"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (j JSON)", []),
        ("INSERT INTO t VALUES (%s)", ['{"a": 1, "b": "x"}']),
        ("SELECT j FROM t", []),
    ])


def test_blob_column(mysql_pair):
    """DDL | BLOB column: bytes parameter in, bytes out"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (b BLOB)", []),
        ("INSERT INTO t VALUES (%s)", [b"\x00\x01\xff"]),
        ("SELECT b FROM t", []),
    ])


def test_alter_modify_change(mysql_pair):
    """DDL | ALTER TABLE MODIFY COLUMN / CHANGE COLUMN"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1)", []),
        ("ALTER TABLE t MODIFY COLUMN name VARCHAR(100) NOT NULL", [], {"ignore": "rowcount"}),  # MySQL counts rebuilt rows
        ("ALTER TABLE t CHANGE COLUMN score points INT", [], {"ignore": "rowcount"}),
        ("SELECT name, points FROM t", []),
    ])


def test_alter_rename_column(mysql_pair):
    """DDL | ALTER TABLE RENAME COLUMN"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1)", []),
        ("ALTER TABLE t RENAME COLUMN score TO points", []),
        ("SELECT name, points FROM t", []),
    ])


def test_rename_table(mysql_pair):
    """DDL | RENAME TABLE / ALTER TABLE ... RENAME TO"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("RENAME TABLE t TO o", []),
        ("SELECT name FROM o", []),
        ("ALTER TABLE o RENAME TO t", []),
        ("SELECT name FROM t", []),
    ])


def test_create_table_as_select(mysql_pair):
    """DDL | CREATE TABLE ... AS SELECT"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("CREATE TABLE o AS SELECT name, score FROM t WHERE score > 1", [], {"ignore": "rowcount"}),
        ("SELECT name, score FROM o", []),
        ("DROP TABLE o", []),
    ])


def test_view(mysql_pair):
    """DDL | CREATE VIEW / DROP VIEW"""
    mysql_pair([
        ("DROP VIEW IF EXISTS v", []), (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("CREATE VIEW v AS SELECT name FROM t WHERE score > 1", []),
        ("SELECT name FROM v", []),
        ("DROP VIEW v", []),
    ])


def test_multi_row_insert(mysql_pair):
    """DML | Multi-row INSERT: rowcount and lastrowid (first id)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b'), ('c')", []),
        ("INSERT INTO t (name) VALUES ('d'), ('e')", []),
        ("SELECT id, name FROM t ORDER BY id", []),
    ])


def test_executemany(mysql_pair):
    """DML | executemany: rowcount is the total"""
    mysql_pair([(T, []), (USERS, [])])
    mysql_pair.many("INSERT INTO t (name, score) VALUES (%s, %s)", [("a", 1), ("b", 2), ("c", 3)])
    mysql_pair([("SELECT id, name, score FROM t ORDER BY id", [])])


def test_default_values(mysql_pair):
    """DML | INSERT with omitted columns and the DEFAULT keyword"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("INSERT INTO t (name, score) VALUES ('b', DEFAULT)", []),
        ("SELECT name, score, email FROM t ORDER BY id", []),
    ])


def test_insert_select(mysql_pair):
    """DML | INSERT ... SELECT"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("CREATE TABLE o (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50))", []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("INSERT INTO o (name) SELECT name FROM t ORDER BY id", []),
        ("SELECT id, name FROM o ORDER BY id", []),
        ("DROP TABLE o", []),
    ])


def test_update_join(mysql_pair):
    """DML | UPDATE t JOIN o ... SET"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("CREATE TABLE o (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, qty INT)", []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("INSERT INTO o (user_id, qty) VALUES (1, 5)", []),
        ("UPDATE t JOIN o ON o.user_id = t.id SET t.score = o.qty", []),
        ("SELECT name, score FROM t ORDER BY id", []),
        ("DROP TABLE o", []),
    ])


def test_delete_join(mysql_pair):
    """DML | DELETE t FROM t JOIN o ..."""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("CREATE TABLE o (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT)", []),
        ("INSERT INTO t (name) VALUES ('a'), ('b')", []),
        ("INSERT INTO o (user_id) VALUES (1)", []),
        ("DELETE t FROM t JOIN o ON o.user_id = t.id", []),
        ("SELECT name FROM t", []),
        ("DROP TABLE o", []),
    ])


@pytest.mark.xfail(not LAMBDA_UPDATE_DELETE_LIMIT, run=False, strict=True,
                   reason="task 22: the server's SQLite is built without ENABLE_UPDATE_DELETE_LIMIT")
def test_update_delete_order_limit(mysql_pair):
    """DML | UPDATE / DELETE ... ORDER BY ... LIMIT | task 22"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name) VALUES ('a'), ('b'), ('c')", []),
        ("UPDATE t SET score = 1 ORDER BY id LIMIT 1", []),
        ("DELETE FROM t ORDER BY id DESC LIMIT 1", []),
        ("SELECT name, score FROM t ORDER BY id", []),
    ])


def test_null_ordering(mysql_pair):
    """Queries | NULLs sort first ascending and last descending"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, email) VALUES ('a', 'x'), ('b', NULL), ('c', 'y')", []),
        ("SELECT name FROM t ORDER BY email, name", []),
        ("SELECT name FROM t ORDER BY email DESC, name", []),
    ])


def test_cross_and_self_join(mysql_pair):
    """Queries | CROSS JOIN, self join with aliases, RIGHT JOIN"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("SELECT x.name AS l, y.name AS r FROM t x CROSS JOIN t y ORDER BY l, r", []),
        ("SELECT x.name AS l, y.name AS r FROM t x JOIN t y ON y.score = x.score + 1", []),
        ("SELECT x.name AS l, y.name AS r FROM t x RIGHT JOIN t y ON y.score = x.score + 1 ORDER BY r", []),
    ])


def test_derived_table_and_cte(mysql_pair):
    """Queries | Derived table in FROM, WITH ... AS, WITH RECURSIVE"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2), ('c', 3)", []),
        ("SELECT x.name FROM (SELECT name FROM t WHERE score > 1) AS x ORDER BY x.name", []),
        ("WITH top AS (SELECT name, score FROM t WHERE score >= 2) SELECT name FROM top ORDER BY score DESC", []),
        ("WITH RECURSIVE n (x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 4) SELECT x FROM n", []),
    ])


def test_window_functions(mysql_pair):
    """Queries | Window functions: ROW_NUMBER, RANK, COUNT(*) OVER (PARTITION BY)"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('a', 2), ('b', 2)", []),
        ("SELECT name, score, ROW_NUMBER() OVER (ORDER BY id) AS rn, RANK() OVER (ORDER BY score DESC) AS rk, "
         "COUNT(*) OVER (PARTITION BY name) AS n FROM t ORDER BY id", []),
    ])


@pytest.mark.xfail(LAMBDA_SQLITE < (3, 44), run=False, strict=True,
                   reason="task 22: ORDER BY inside GROUP_CONCAT needs SQLite 3.44")
def test_group_concat(mysql_pair):
    """Queries | GROUP_CONCAT with ORDER BY and SEPARATOR | task 22"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('b', 1), ('a', 1), ('c', 2)", []),
        ("SELECT score, GROUP_CONCAT(name ORDER BY name SEPARATOR '|') AS names FROM t GROUP BY score ORDER BY score", []),
    ])


def test_integer_division(mysql_pair):
    """Queries | 7 / 2 is 3.5000 (Decimal) in MySQL, 7 DIV 2 is 3"""
    mysql_pair([("SELECT 7 / 2 AS d, 7 DIV 2 AS i, 7 % 2 AS m", [])])


def test_large_result(mysql_pair):
    """Queries | 1200-row result comes back complete (paging is transparent)"""
    values = ", ".join(f"('row-{i:05d}', {i})" for i in range(1200))
    mysql_pair([
        (T, []), (USERS, []),
        (f"INSERT INTO t (name, score) VALUES {values}", []),
        ("SELECT id, name, score FROM t ORDER BY id", []),
    ])


def test_string_functions_more(mysql_pair):
    """Functions | CHAR_LENGTH, LOCATE, LPAD, RPAD, REPEAT, SUBSTRING_INDEX (±1), LTRIM, RTRIM"""
    mysql_pair([
        ("SELECT CHAR_LENGTH('héllo') AS cl, LOCATE('l', 'hello') AS lo, LPAD('5', 3, '0') AS lp, RPAD('5', 3, '0') AS rp, "
         "REPEAT('ab', 2) AS rp2, SUBSTRING_INDEX('a.b.c', '.', 1) AS si, SUBSTRING_INDEX('a.b.c', '.', -1) AS si2, "
         "LTRIM('  x ') AS lt, RTRIM(' x  ') AS rt", []),
    ])


def test_math_functions(mysql_pair):
    """Functions | ROUND, FLOOR, CEIL, POWER, SQRT, SIGN, TRUNCATE, GREATEST, LEAST"""
    mysql_pair([
        ("SELECT ROUND(2.567E0, 2) AS r, ROUND(2.4E0) AS r0, FLOOR(2.5E0) AS f, CEIL(2.1E0) AS c, POWER(2E0, 3) AS p, "
         "SQRT(16E0) AS s, SIGN(-3) AS sg, TRUNCATE(2.567E0, 1) AS tr, GREATEST(1, 5, 3) AS g, LEAST(1, 5, 3) AS l", []),
    ])


def test_control_functions(mysql_pair):
    """Functions | IF(), IFNULL, NULLIF, COALESCE"""
    mysql_pair([
        ("SELECT IF(1 > 0, 'y', 'n') AS i, IF(0, 1, 2) AS i2, IFNULL(NULL, 3) AS n, NULLIF(1, 1) AS z, COALESCE(NULL, NULL, 'c') AS c", []),
    ])


def test_aggregate_functions(mysql_pair):
    """Functions | COUNT, COUNT(DISTINCT), MIN, MAX, SUM, AVG values"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('a', 2), ('b', 3)", []),
        ("SELECT COUNT(*) AS c, COUNT(DISTINCT name) AS cd, MIN(score) AS mn, MAX(score) AS mx, "
         "SUM(score) = 6 AS s_ok, AVG(score) = 2 AS a_ok FROM t", []),
    ])


def test_now_functions(mysql_pair):
    """Functions | NOW(), CURDATE(), CURTIME(), CURRENT_TIMESTAMP, CURRENT_DATE values"""
    mysql_pair([
        ("SELECT NOW() >= '2026-01-01' AS n, DATE(NOW()) = CURDATE() AS d, CURTIME() IS NOT NULL AS t, "
         "CURRENT_TIMESTAMP >= '2026-01-01' AS ct, CURRENT_DATE = CURDATE() AS cd", []),
    ])


def test_date_parts(mysql_pair):
    """Functions | YEAR, MONTH, DAY, HOUR, MINUTE, SECOND, DAYOFWEEK, DATE(), TIME()"""
    mysql_pair([
        ("SELECT YEAR('2026-03-04 05:06:07') AS y, MONTH('2026-03-04 05:06:07') AS m, DAY('2026-03-04 05:06:07') AS d, "
         "HOUR('2026-03-04 05:06:07') AS h, MINUTE('2026-03-04 05:06:07') AS mi, SECOND('2026-03-04 05:06:07') AS s, "
         "DAYOFWEEK('2026-03-04') AS dw, DATE('2026-03-04 05:06:07') = '2026-03-04' AS d_ok, "
         "TIME('2026-03-04 05:06:07') = '05:06:07' AS t_ok", []),
    ])


def test_date_diff(mysql_pair):
    """Functions | DATEDIFF, TIMESTAMPDIFF"""
    mysql_pair([
        ("SELECT DATEDIFF('2026-01-10', '2026-01-01') AS dd, "
         "TIMESTAMPDIFF(HOUR, '2026-01-01 00:00:00', '2026-01-02 06:00:00') AS th, "
         "TIMESTAMPDIFF(DAY, '2026-01-01', '2026-03-01') AS td", []),
    ])


def test_date_format(mysql_pair):
    """Functions | DATE_FORMAT with %Y %m %d %H %i %s"""
    mysql_pair([
        ("SELECT DATE_FORMAT('2026-03-04 05:06:07', '%Y/%m/%d %H:%i:%s') AS f, DATE_FORMAT('2026-03-04', '%Y-%m') AS ym", []),
    ])


def test_unix_time(mysql_pair):
    """Functions | UNIX_TIMESTAMP, FROM_UNIXTIME values"""
    mysql_pair([
        ("SELECT UNIX_TIMESTAMP() > 1700000000 AS u, FROM_UNIXTIME(0) = '1970-01-01 00:00:00' AS f, "
         "FROM_UNIXTIME(86400) >= '1970-01-02' AS f2", []),
    ])


def test_cast(mysql_pair):
    """Functions | CAST(x AS SIGNED / CHAR / DATE), CONVERT"""
    mysql_pair([
        ("SELECT CAST('12' AS SIGNED) AS i, CAST(12 AS CHAR) AS s, CONVERT('7', SIGNED) AS c, "
         "CAST('2026-01-02' AS DATE) = '2026-01-02' AS d_ok", []),
    ])


@pytest.mark.xfail(strict=True, reason="ROUND(2.5E0) is 2.0 in MySQL (half to even for floats) and 3.0 in SQLite")
@pytest.mark.quirk
def test_round_half_float(mysql_pair):
    """Functions | ROUND of a float half-way value | MySQL half-to-even, SQLite half-away"""
    mysql_pair([("SELECT ROUND(2.5E0) AS r", [])])


@pytest.mark.xfail(strict=True, reason="CAST(1.5 AS SIGNED) is 2 in MySQL (rounds) and 1 in SQLite (truncates)")
@pytest.mark.quirk
def test_cast_float_rounding(mysql_pair):
    """Functions | CAST(float AS SIGNED) rounds | MySQL rounds, SQLite truncates"""
    mysql_pair([("SELECT CAST(1.5E0 AS SIGNED) AS rnd", [])])


def test_json_functions(mysql_pair):
    """Functions | JSON_EXTRACT, ->, ->>, JSON_UNQUOTE, JSON_OBJECT"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (j JSON)", []),
        ("INSERT INTO t VALUES ('{\"a\": 1, \"b\": \"x\"}')", []),
        ("SELECT j->>'$.b' AS b, JSON_UNQUOTE(JSON_EXTRACT(j, '$.b')) AS b2, JSON_EXTRACT(j, '$.b') AS q, "
         "j->>'$.a' = 1 AS a_ok, j->>'$.a' > 0 AS gt FROM t", []),
        ("SELECT JSON_EXTRACT(JSON_OBJECT('k', 1), '$.k') AS k", []),   # MySQL pretty-prints JSON ({"k": 1}), SQLite does not
    ])


def test_misc_functions(mysql_pair):
    """Functions | VERSION(), DATABASE(), RAND(), UUID() shape"""
    mysql_pair([
        ("SELECT VERSION() LIKE '8.%%' AS v, DATABASE() AS db, RAND() BETWEEN 0 AND 1 AS r, LENGTH(UUID()) AS u", []),
    ])


def test_parameter_types(mysql_pair):
    """Types | Parameter binding: int, float, str, None, bool"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (i INT, f DOUBLE, s VARCHAR(10), n INT, b TINYINT)", []),
        ("INSERT INTO t VALUES (%s, %s, %s, %s, %s)", [42, 1.5, "text", None, True]),
        ("SELECT i, f, s, n, b FROM t", []),
    ])


def test_datetime_and_decimal_params(mysql_pair):
    """Types | datetime / date / Decimal parameters"""
    import datetime
    import decimal
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (dt VARCHAR(30), d VARCHAR(30), x VARCHAR(30))", []),
        ("INSERT INTO t VALUES (%s, %s, %s)", [datetime.datetime(2026, 1, 2, 3, 4, 5), datetime.date(2026, 1, 2), decimal.Decimal("1.25")]),
        ("SELECT dt, d, x FROM t", []),
    ])


def test_result_scalar_types(mysql_pair):
    """Types | Result types: INT → int, VARCHAR → str, DOUBLE / FLOAT → float, NULL → None"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (i INT, s VARCHAR(10), d DOUBLE, f FLOAT, n INT)", []),
        ("INSERT INTO t VALUES (1, 'x', 1.5, 2.5, NULL)", []),
        ("SELECT i, s, d, f, n FROM t", []),
    ])


def test_error_table_exists(mysql_pair):
    """Constraints & errors | 1050 table already exists, 1051 unknown table on DROP"""
    mysql_pair([
        (T, []), (USERS, []),
        (USERS, []),                    # 1050
        ("DROP TABLE nope", []),        # 1051
    ])


def test_check_constraint(mysql_pair):
    """Constraints & errors | CHECK constraint violation (3819)"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (n INT, CHECK (n > 0))", []),
        ("INSERT INTO t VALUES (1)", []),
        ("INSERT INTO t VALUES (-1)", []),     # 3819
    ])


def test_strict_type_error(mysql_pair):
    """Constraints & errors | Strict mode: 'abc' into an INT column raises 1366"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (n INT)", []),
        ("INSERT INTO t VALUES ('abc')", []),  # 1366
    ])


def test_transaction_commit(mysql_pair):
    """Transactions | START TRANSACTION ... COMMIT"""
    mysql_pair([
        (T, []), (USERS, []),
        ("START TRANSACTION", []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("COMMIT", []),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_transaction_rollback(mysql_pair):
    """Transactions | START TRANSACTION ... ROLLBACK discards the writes"""
    mysql_pair([
        (T, []), (USERS, []),
        ("START TRANSACTION", []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("ROLLBACK", []),
        ("SELECT COUNT(*) AS c FROM t", []),
    ])


def test_transaction_ddl_implicit_commit(mysql_pair):
    """Transactions | DDL inside a transaction commits implicitly (MySQL semantics)"""
    mysql_pair([
        (T, []), ("DROP TABLE IF EXISTS o", []), (USERS, []),
        ("START TRANSACTION", []),
        ("INSERT INTO t (name) VALUES ('a')", []),
        ("CREATE TABLE o (id INT)", []),
        ("ROLLBACK", []),
        ("SELECT COUNT(*) AS c FROM t", []),
        ("SELECT COUNT(*) AS c FROM o", []),
        ("DROP TABLE o", []),
    ])


def test_show_databases_and_columns(mysql_pair):
    """Metadata | SHOW DATABASES, SHOW COLUMNS FROM t (field names and keys)"""
    mysql_pair([(T, []), (USERS, [])])
    real, ours = mysql_pair.both("SHOW DATABASES")
    assert "compat" in {r["Database"] for r in real.rows} and "compat" in {r["Database"] for r in ours.rows}
    real, ours = mysql_pair.both("SHOW COLUMNS FROM t")
    assert [(r["Field"], r["Key"]) for r in real.rows] == [(r["Field"], r["Key"]) for r in ours.rows]


def test_show_create_table(mysql_pair):
    """Metadata | SHOW CREATE TABLE returns the original column definitions"""
    mysql_pair([(T, []), (USERS, [])])
    real, ours = mysql_pair.both("SHOW CREATE TABLE t")
    for side in (real, ours):
        ddl = side.rows[0]["Create Table"]
        assert side.rows[0]["Table"] == "t"
        assert "AUTO_INCREMENT" in ddl and "varchar(50)" in ddl.lower() and "NOT NULL" in ddl


def test_show_index(mysql_pair):
    """Metadata | SHOW INDEX FROM t (key and column names)"""
    mysql_pair([(T, []), (USERS, []), ("CREATE INDEX idx_score ON t (score)", [])])
    real, ours = mysql_pair.both("SHOW INDEX FROM t")
    assert {(r["Key_name"], r["Column_name"]) for r in real.rows} == {(r["Key_name"], r["Column_name"]) for r in ours.rows}


def test_information_schema(mysql_pair):
    """Metadata | information_schema.tables / columns"""
    mysql_pair([
        (T, []), (USERS, []),
        ("SELECT COUNT(*) AS c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = 't'", []),
        ("SELECT column_name AS n FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 't' "
         "ORDER BY ordinal_position", []),
    ])


def test_session_statements(mysql_pair):
    """Metadata | USE db, SET NAMES, SET SESSION sql_mode are accepted"""
    mysql_pair([
        ("USE compat", []),
        ("SET NAMES utf8mb4", []),
        ("SET SESSION sql_mode = 'STRICT_TRANS_TABLES'", []),
        ("SET time_zone = '+00:00'", []),
        ("SELECT 1 AS ok", []),
    ])


# ---- unsupported by design: MySQL runs these, s3rds refuses them with NotSupportedError ----

@pytest.mark.unsupported("stored procedures run inside the server; use application code")
def test_procedure_refused(mysql_pair):
    """Unsupported | CREATE PROCEDURE / CALL | server-side code; use the application"""
    mysql_pair.both("DROP PROCEDURE IF EXISTS p")
    mysql_pair.unsupported("CREATE PROCEDURE p() SELECT 1")
    mysql_pair.both("DROP PROCEDURE IF EXISTS p")


@pytest.mark.unsupported("no session state between requests")
def test_user_variables_refused(mysql_pair):
    """Unsupported | SET @x := 1 user variables | no session state between requests"""
    mysql_pair.unsupported("SET @x := 1")


@pytest.mark.unsupported("full-text search needs a real server")
def test_fulltext_refused(mysql_pair):
    """Unsupported | FULLTEXT index, MATCH ... AGAINST | full-text search needs a real server"""
    mysql_pair([(T, [])])
    mysql_pair.unsupported("CREATE TABLE t (a TEXT, FULLTEXT (a))")


@pytest.mark.unsupported("users are the endpoint's Basic-auth credentials")
def test_grant_refused(mysql_pair):
    """Unsupported | CREATE USER / GRANT | users are the endpoint credentials"""
    mysql_pair.both("DROP USER IF EXISTS 'compat_u'@'%'")
    mysql_pair.unsupported("CREATE USER 'compat_u'@'%' IDENTIFIED BY 'x'")
    mysql_pair.both("DROP USER IF EXISTS 'compat_u'@'%'")


@pytest.mark.unsupported("one SQLite file per database; no partitions")
def test_partition_refused(mysql_pair):
    """Unsupported | PARTITION BY | one file per database"""
    mysql_pair([(T, [])])
    mysql_pair.unsupported("CREATE TABLE t (id INT PRIMARY KEY) PARTITION BY HASH(id) PARTITIONS 2")

@pytest.mark.unsupported("row locks need a real server; s3rds has one writer and no sessions")
def test_select_for_update_refused(mysql_pair):
    """Unsupported | SELECT ... FOR UPDATE | row locks need a real server"""
    mysql_pair([(T, []), (USERS, [])])
    mysql_pair.unsupported("SELECT * FROM t WHERE id = 1 FOR UPDATE")
    mysql_pair.unsupported("SELECT * FROM t WHERE id = 1 LOCK IN SHARE MODE")


@pytest.mark.unsupported("stored procedures / triggers run inside the server; use application code")
def test_trigger_refused(mysql_pair):
    """Unsupported | CREATE TRIGGER | server-side code; use the application"""
    mysql_pair([(T, []), (USERS, [])])
    mysql_pair.unsupported("CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET NEW.score = 1")


@pytest.mark.unsupported("table locks need a real server")
def test_lock_tables_refused(mysql_pair):
    """Unsupported | LOCK TABLES | table locks need a real server"""
    mysql_pair([(T, []), (USERS, [])])
    mysql_pair.unsupported("LOCK TABLES t WRITE")
    real, ours = mysql_pair.both("UNLOCK TABLES")  # release the reference side's lock
    assert real is ours or real.error is None


# ---- known gaps: each marker names the task in the MySQL spec that closes it ----

def test_result_datetime_types(mysql_pair):
    """Types | Result types: DATETIME → datetime, DATE → date, TIME → timedelta"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (dt DATETIME, d DATE, tm TIME)", []),
        ("INSERT INTO t VALUES ('2026-01-02 03:04:05', '2026-01-02', '03:04:05')", []),
        ("SELECT dt, d, tm FROM t", []),
    ])


def test_result_decimal_types(mysql_pair):
    """Types | Result types: DECIMAL → Decimal, TINYINT(1) → int, BIGINT → int"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (x DECIMAL(10,2), b TINYINT(1), big BIGINT)", []),
        ("INSERT INTO t VALUES (1.25, 1, 9007199254740993)", []),
        ("SELECT x, b, big FROM t", []),
    ])


def test_result_blob_type(mysql_pair):
    """Types | BLOB results come back as bytes"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (b BLOB)", []),
        ("INSERT INTO t VALUES (X'0001FF')", []),
        ("SELECT b FROM t", []),
    ])


def test_aggregate_types(mysql_pair):
    """Types | Aggregate result types: COUNT(*) int, SUM(INT) Decimal"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("SELECT COUNT(*) AS c, SUM(score) AS s FROM t", []),
    ])


def test_date_add_keeps_time(mysql_pair):
    """Functions | DATE_ADD / DATE_SUB with INTERVAL keep the time part"""
    mysql_pair([
        ("SELECT DATE_ADD('2026-01-01 10:30:00', INTERVAL 1 DAY) AS d", []),
    ])


def test_inline_index_exists(mysql_pair):
    """DDL | Inline KEY / INDEX in CREATE TABLE creates the index"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(100), KEY idx_title (title), UNIQUE KEY uq (title, id))", []),
    ])
    real, ours = mysql_pair.both("SHOW INDEX FROM t")
    assert {(r["Key_name"], r["Column_name"]) for r in real.rows} == {(r["Key_name"], r["Column_name"]) for r in ours.rows}


def test_now_type(mysql_pair):
    """Types | NOW() / CURDATE() return datetime / date objects"""
    import datetime
    real, ours = mysql_pair.both("SELECT NOW() AS n, CURDATE() AS d, CURTIME() AS t")
    for side in (real, ours):
        assert side.error is None, side.error
        row = side.rows[0]
        assert type(row["n"]) is datetime.datetime and type(row["d"]) is datetime.date and type(row["t"]) is datetime.timedelta, row


@pytest.mark.xfail(strict=True, reason="MySQL strict mode reports 1265 for a bad ENUM value; s3rds enforces it with a CHECK constraint (3819)")
@pytest.mark.quirk
def test_enum_error_code(mysql_pair):
    """Constraints & errors | ENUM rejects values outside the list with MySQL's error code | MySQL 1265, s3rds 3819 (CHECK)"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (s ENUM('a','b'))", []),
        ("INSERT INTO t (s) VALUES ('zzz')", []),
    ])


@pytest.mark.xfail(strict=True, reason="MySQL distinguishes an omitted NOT NULL column (1364) from an explicit NULL (1048); SQLite cannot, s3rds reports 1048")
@pytest.mark.quirk
def test_missing_column_error_code(mysql_pair):
    """Constraints & errors | Omitting a NOT NULL column without default raises 1364 | MySQL 1364, s3rds 1048"""
    mysql_pair([(T, []), (USERS, []), ("INSERT INTO t (email) VALUES ('c')", [])])


@pytest.mark.xfail(strict=True, reason="for an upsert that updated an existing row MySQL reports rowcount 2 / lastrowid 0; s3rds reports 1 / the row id")
@pytest.mark.quirk
def test_upsert_rowcount_quirk(mysql_pair):
    """DML | ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | MySQL 2 / 0, s3rds 1 / row id"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (id, name) VALUES (1, 'a')", []),
        ("INSERT INTO t (id, name) VALUES (1, 'b') ON DUPLICATE KEY UPDATE name = VALUES(name)", []),
    ])


def test_describe(mysql_pair):
    """Metadata | DESCRIBE shows MySQL type names"""
    mysql_pair([
        (T, []), (USERS, []),
        ("DESCRIBE t", []),
    ])
