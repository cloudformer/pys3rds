"""pys3mysql: MySQL dialect end-to-end against the real server code (in-process, fake S3).

Needs sqlglot (pip install sqlglot). The optional comparison tests at the bottom additionally
run the same statements on a real MySQL when MYSQL_DSN is set, e.g.
    MYSQL_DSN=mysql://root:pw@127.0.0.1:3306/test pytest tests/test_pys3mysql.py
"""
import base64
import json
import os

import pytest

sqlglot = pytest.importorskip("sqlglot")

import lambda_function as lf  # noqa: E402
import pys3mysql  # noqa: E402
from pys3mysql import (IntegrityError, NotSupportedError, OperationalError,  # noqa: E402
                       ProgrammingError, _bind, _translate)

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()


@pytest.fixture
def conn(fake_s3, monkeypatch, bucket):
    """A pys3mysql connection whose HTTP layer calls the lambda handler in-process."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    c = pys3mysql.connect(host="in-process.test", user="admin", password="pw", database="d")

    def post(path, payload):
        event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
                 "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
        resp = lf.lambda_handler(event, None)
        data = json.loads(resp["body"])
        if resp["statusCode"] >= 400:
            raise pys3mysql.pys3sqlite.Error(resp["statusCode"], data.get("error", ""))
        return data

    monkeypatch.setattr(c, "_post", post)
    monkeypatch.setattr(c, "_list_databases", lambda: ["d"])
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
    assert "INSERT OR IGNORE" in _translate("INSERT IGNORE INTO t (id) VALUES (1)").upper()
    out = _translate("SELECT * FROM `t` LIMIT 5, 10").upper()
    assert "LIMIT 10 OFFSET 5" in out and "`" not in out


def test_translate_syntax_error_is_1064():
    with pytest.raises(ProgrammingError) as e:
        _translate("SELEC * FROM t")
    assert e.value.code == 1064


def test_unsupported_features_raise():
    c = pys3mysql.Connection("https://x", "d")
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
    with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
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
    with pytest.raises(ProgrammingError) as e:
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
    assert desc[1] == {"Field": "name", "Type": "text", "Null": "NO", "Key": "", "Default": None, "Extra": ""}
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


# ---- optional: same statements on a real MySQL (pymysql) vs pys3mysql ----

CASES = [
    "INSERT INTO cmp (name, score) VALUES ('alice', 10)",
    "INSERT INTO cmp (name, score) VALUES ('Bob', 20)",
    "INSERT IGNORE INTO cmp (id, name, score) VALUES (1, 'dup', 0)",
    "INSERT INTO cmp (id, name, score) VALUES (2, 'bob2', 25) ON DUPLICATE KEY UPDATE score = VALUES(score)",
    "SELECT name, score FROM cmp WHERE name = 'ALICE'",
    "SELECT name, score FROM cmp ORDER BY score DESC LIMIT 1, 1",
    "SELECT COUNT(*) AS n, SUM(score) AS s FROM cmp",
    "SELECT IFNULL(NULL, 'd') AS v, CONCAT_WS('-', 'a', 'b') AS w",
]


@pytest.mark.skipif(not os.environ.get("MYSQL_DSN"), reason="set MYSQL_DSN to compare against a real MySQL")
def test_compare_with_real_mysql(conn):
    pymysql = pytest.importorskip("pymysql")
    from urllib.parse import urlparse
    u = urlparse(os.environ["MYSQL_DSN"])
    real = pymysql.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password,
                           database=u.path.lstrip("/"), autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    ddl = "CREATE TABLE cmp (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50), score INT)"
    with real.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS cmp")
        cur.execute(ddl)
    conn.execute(ddl)
    report = []
    for sql in CASES:
        with real.cursor() as cur:
            cur.execute(sql)
            expected = [dict(r) for r in cur.fetchall()] if cur.description else cur.rowcount
        r = conn.execute(sql)
        got = [dict(zip(r["columns"], row)) for row in r["rows"]] if r["columns"] else r["rowcount"]
        # MySQL returns Decimal for SUM; normalise numbers
        norm = lambda v: [{k: (float(x) if isinstance(x, (int, float)) or type(x).__name__ == "Decimal" else x)
                           for k, x in row.items()} for row in v] if isinstance(v, list) else v
        report.append((sql, norm(expected) == norm(got), expected, got))
    bad = [r for r in report if not r[1]]
    assert not bad, "\n".join(f"{s}\n  mysql: {e}\n  pys3mysql: {g}" for s, _, e, g in bad)
