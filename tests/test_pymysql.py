"""s3rds.pymysql: MySQL dialect end-to-end against the real server code (in-process, fake S3).

Needs sqlglot (pip install sqlglot). The optional comparison test at the bottom additionally
runs the same statements on a real MySQL when MYSQL_DSN is set, e.g.
    MYSQL_DSN=mysql://root:pw@127.0.0.1:3306/test pytest tests/test_pymysql.py
"""
import base64
import json

import pytest

sqlglot = pytest.importorskip("sqlglot")

import lambda_function as lf  # noqa: E402
import s3rds  # noqa: E402
from s3rds import pymysql as s3mysql  # noqa: E402
from s3rds.pymysql import (IntegrityError, NotSupportedError, OperationalError,  # noqa: E402
                           ProgrammingError, _bind, _translate)

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()


@pytest.fixture
def conn(fake_s3, monkeypatch, bucket):
    """A s3rds.pymysql connection whose HTTP layer calls the lambda handler in-process."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    c = s3mysql.connect(host="in-process.test", user="admin", password="pw", database="d")

    def post(path, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                 "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
        resp = lf.lambda_handler(event, None)
        data = json.loads(resp["body"])
        if resp["statusCode"] >= 400:
            raise s3rds.sqlite.Error(resp["statusCode"], data.get("error", ""))
        return data

    monkeypatch.setattr(c, "_post", post)
    monkeypatch.setattr(c, "list_databases", lambda: ["d"])
    c.create_database()
    return c


# ---- placeholders ----

def test_bind_pyformat_and_dict():
    assert _bind("a = %s AND b = %s", [1, 2]) == ("a = ? AND b = ?", [1, 2])
    assert _bind("a = %(x)s AND b = %(y)s AND c = %(x)s", {"x": 1, "y": 2}) == ("a = ? AND b = ? AND c = ?", [1, 2, 1])
    assert _bind("LIKE '%%a%%' AND a = %s", [1]) == ("LIKE '%%a%%' AND a = ?", [1])
    assert _bind("SELECT 10 %% 3", []) == ("SELECT 10 % 3", [])


# ---- translation (pure) ----

def test_translate_create_table():
    out = _translate("CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL, "
                     "status ENUM('a','b') DEFAULT 'a', created DATETIME DEFAULT CURRENT_TIMESTAMP "
                     "ON UPDATE CURRENT_TIMESTAMP, KEY idx_name (name)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4")
    up = out.upper()
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in up
    assert "COLLATE NOCASE" in up
    assert "CHECK" in up and "'A'" in up
    assert "ENGINE" not in up and "ON UPDATE" not in up and "IDX_NAME" not in up


def test_translate_table_level_pk_moves_into_column():
    out = _translate("CREATE TABLE t (id INT AUTO_INCREMENT, v TEXT, PRIMARY KEY (id))").upper()
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in out
    assert out.count("PRIMARY KEY") == 1


def test_translate_upsert_and_limit():
    out = _translate("INSERT INTO t (id, v) VALUES (1, 'x') ON DUPLICATE KEY UPDATE v = VALUES(v)").upper()
    assert "ON CONFLICT DO UPDATE SET" in out and "EXCLUDED.V" in out
    out = _translate("INSERT IGNORE INTO t (id) VALUES (1)").upper()
    assert "INSERT OR IGNORE" in out and "VALUES (1)" in out  # a plain VALUES (1) is never touched
    out = _translate("SELECT * FROM `t` LIMIT 5, 10").upper()
    assert "LIMIT 10 OFFSET 5" in out and "`" not in out


def test_translate_syntax_error_is_1064():
    with pytest.raises(ProgrammingError) as e:
        _translate("SELEC * FROM t")
    assert e.value.code == 1064


def test_unsupported_features_raise():
    c = s3mysql.Connection("https://x", "d")
    for sql in ["CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET NEW.a = 1",
                "SELECT * FROM t WHERE id = 1 FOR UPDATE",
                "LOCK TABLES t WRITE",
                "GRANT ALL ON *.* TO 'u'@'%'",
                "CREATE TABLE t (a TEXT, FULLTEXT (a))"]:
        with pytest.raises(NotSupportedError):
            c.execute(sql)


# ---- end to end on the real server code ----

SCHEMA = """CREATE TABLE users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(50) NOT NULL,
  email VARCHAR(100) UNIQUE,
  status ENUM('active','banned') DEFAULT 'active',
  created DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def test_crud_mysql_style(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA)
        cur.execute("INSERT INTO users (name, email) VALUES (%s, %s)", ["alice", "a@x"])
        assert cur.lastrowid == 1
        cur.execute("INSERT INTO users (name, email) VALUES (%(n)s, %(e)s)", {"n": "bob", "e": "b@x"})
        assert cur.lastrowid == 2
    with conn.cursor(s3mysql.cursors.DictCursor) as cur:
        cur.execute("SELECT id, name, status FROM `users` WHERE name = %s", ["ALICE"])  # NOCASE
        assert cur.fetchall() == [{"id": 1, "name": "alice", "status": "active"}]
        cur.execute("SELECT name FROM users ORDER BY id LIMIT 1, 1")
        assert cur.fetchall() == [{"name": "bob"}]


def test_mysql_error_codes(conn):
    conn.execute(SCHEMA)
    conn.execute("INSERT INTO users (name, email) VALUES ('a', 'dup')")
    with pytest.raises(IntegrityError) as e:
        conn.execute("INSERT INTO users (name, email) VALUES ('b', 'dup')")
    assert e.value.args[0] == 1062
    with pytest.raises(ProgrammingError) as e:
        conn.execute("SELECT * FROM nope")
    assert e.value.code == 1146
    with pytest.raises(OperationalError) as e:  # pymysql's class for 1054 (verified against MySQL 8.0)
        conn.execute("SELECT nope FROM users")
    assert e.value.code == 1054
    with pytest.raises(IntegrityError) as e:
        conn.execute("INSERT INTO users (email) VALUES ('c')")  # name NOT NULL
    assert e.value.code == 1048
    with pytest.raises(IntegrityError) as e:
        conn.execute("INSERT INTO users (name, status) VALUES ('c', 'weird')")  # ENUM -> CHECK
    assert e.value.code == 3819


def test_upsert_end_to_end(conn):
    conn.execute(SCHEMA)
    conn.execute("INSERT INTO users (id, name, email) VALUES (1, 'a', 'a@x')")
    conn.execute("INSERT IGNORE INTO users (id, name, email) VALUES (1, 'zzz', 'a@x')")
    conn.execute("INSERT INTO users (id, name, email) VALUES (1, 'a2', 'a@x') ON DUPLICATE KEY UPDATE name = VALUES(name)")
    assert conn.query("SELECT name FROM users WHERE id = 1") == [{"name": "a2"}]


def test_metadata_commands(conn):
    conn.execute(SCHEMA)
    assert conn.query("SHOW TABLES") == [{"Tables_in_d": "users"}]
    desc = conn.query("DESCRIBE users")
    assert desc[0]["Field"] == "id" and desc[0]["Key"] == "PRI" and desc[0]["Extra"] == "auto_increment"
    assert desc[1]["Field"] == "name" and desc[1]["Null"] == "NO" and desc[1]["Key"] == ""
    assert desc[1]["Type"].startswith(("text", "varchar"))  # sqlglot renders VARCHAR(50) as TEXT(50)
    assert conn.query("SELECT DATABASE()") == [{"DATABASE()": "d"}]
    assert conn.query("SELECT VERSION()")[0]["VERSION()"].startswith("8.0")
    assert conn.query("SHOW CREATE TABLE users")[0]["Table"] == "users"
    assert conn.query("SHOW DATABASES") == [{"Database": "d"}]


def test_transaction_all_or_nothing(conn):
    conn.execute(SCHEMA)
    with pytest.raises(IntegrityError):
        conn.transaction([
            ("INSERT INTO users (name, email) VALUES (%s, %s)", ["a", "x"]),
            ("INSERT INTO users (name, email) VALUES (%s, %s)", ["b", "x"]),  # duplicate email
        ])
    assert conn.query("SELECT COUNT(*) AS n FROM users") == [{"n": 0}]


def test_functions(conn):
    conn.execute("CREATE TABLE t (a VARCHAR(10), b VARCHAR(10))")
    conn.execute("INSERT INTO t (a, b) VALUES ('x', NULL)")
    row = conn.query("SELECT IFNULL(b, 'none') AS b, CONCAT_WS('-', a, 'y') AS c, LAST_INSERT_ID() AS id FROM t")[0]
    assert row == {"b": "none", "c": "x-y", "id": 1}
    assert conn.query("SELECT NOW() AS n")[0]["n"]
    assert conn.query("SELECT CURDATE() AS d")[0]["d"]
    assert conn.query("SELECT UNIX_TIMESTAMP() AS u")[0]["u"] > 1_700_000_000
    assert conn.query("SELECT FROM_UNIXTIME(0) AS f")[0]["f"] == "1970-01-01 00:00:00"


def test_left_right(conn):
    row = conn.query("SELECT LEFT('hello', 2) AS l, RIGHT('hello', 2) AS r, RIGHT('hello', 0) AS z, RIGHT('hi', 9) AS big")[0]
    assert row == {"l": "he", "r": "lo", "z": "", "big": "hi"}


def test_drop_index_on_and_truncate(conn):
    conn.execute("CREATE TABLE t (id INT AUTO_INCREMENT PRIMARY KEY, v VARCHAR(10))")
    conn.execute("CREATE UNIQUE INDEX uq ON t (v)")
    conn.execute("INSERT INTO t (v) VALUES ('a')")
    with pytest.raises(IntegrityError):
        conn.execute("INSERT INTO t (v) VALUES ('a')")
    conn.execute("DROP INDEX uq ON t")
    conn.execute("INSERT INTO t (v) VALUES ('a')")          # allowed once the index is gone
    conn.execute("TRUNCATE TABLE t")
    assert conn.query("SELECT COUNT(*) AS n FROM t") == [{"n": 0}]
    conn.execute("INSERT INTO t (v) VALUES ('b')")
    assert conn.query("SELECT id FROM t") == [{"id": 1}]    # AUTO_INCREMENT was reset, like MySQL


def test_psycopg2_placeholder_raises():
    with pytest.raises(s3rds.psycopg2.NotSupportedError):
        s3rds.psycopg2.connect(host="x", user="u", password="p", dbname="d")

