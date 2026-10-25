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


def test_upsert(mysql_pair):
    """DML | INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)"""
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


# ---- unsupported by design: MySQL runs these, s3rds refuses them with NotSupportedError ----

@pytest.mark.unsupported("row locks need a real server; s3rds has one writer and no sessions")
def test_select_for_update_refused(mysql_pair):
    """Unsupported | SELECT ... FOR UPDATE | row locks need a real server"""
    mysql_pair([(T, []), (USERS, [])])
    mysql_pair.unsupported("SELECT * FROM t WHERE id = 1 FOR UPDATE")


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
    real, _ = mysql_pair.both("UNLOCK TABLES")  # release the reference side's lock
    assert real.error is None


# ---- known gaps: each marker names the task in the MySQL spec that closes it ----

@pytest.mark.xfail(strict=True, reason="task 8: DATETIME/DECIMAL come back as str/int, pymysql returns datetime/Decimal")
def test_result_types(mysql_pair):
    """Types | Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int | task 8"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (d DATETIME, x DECIMAL(10,2), b TINYINT(1))", []),
        ("INSERT INTO t VALUES ('2026-01-02 03:04:05', 1.25, 1)", []),
        ("SELECT d, x, b FROM t", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 8: SUM() is Decimal in MySQL, int here")
def test_aggregate_types(mysql_pair):
    """Types | Aggregate result types: COUNT(*) int, SUM(INT) Decimal | task 8"""
    mysql_pair([
        (T, []), (USERS, []),
        ("INSERT INTO t (name, score) VALUES ('a', 1), ('b', 2)", []),
        ("SELECT COUNT(*) AS c, SUM(score) AS s FROM t", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 6: DATE_ADD drops the time part (DATE() instead of datetime())")
def test_date_add_keeps_time(mysql_pair):
    """Functions | DATE_ADD / DATE_SUB with INTERVAL keep the time part | task 6"""
    mysql_pair([
        ("SELECT DATE_ADD('2026-01-01 10:30:00', INTERVAL 1 DAY) AS d", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 7: inline KEY idx (col) is dropped instead of becoming CREATE INDEX")
def test_inline_index_exists(mysql_pair):
    """DDL | Inline KEY / INDEX in CREATE TABLE creates the index | task 7"""
    mysql_pair([
        (T, []),
        ("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(100), KEY idx_title (title))", []),
        ("SELECT COUNT(*) AS n FROM information_schema.statistics WHERE table_name = 't' AND index_name = 'idx_title'", []),
    ])


@pytest.mark.xfail(strict=True, reason="task 9: NOW() returns datetime in pymysql, str here")
def test_now_type(mysql_pair):
    """Types | NOW() / CURDATE() return datetime / date objects | task 9"""
    mysql_pair([("SELECT NOW() IS NOT NULL AS ok, DATE(NOW()) = CURDATE() AS same, NOW() AS n", [])])


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


@pytest.mark.xfail(strict=True, reason="task 12: DESCRIBE shows SQLite type names (text(50)) not MySQL's (varchar(50))")
def test_describe(mysql_pair):
    """Metadata | DESCRIBE shows MySQL type names | task 12"""
    mysql_pair([
        (T, []), (USERS, []),
        ("DESCRIBE t", []),
    ])
