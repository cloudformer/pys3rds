"""Open transactions (connect(autocommit=False), and SQLAlchemy on top): against the real server code
in-process with the fake S3. What the compatibility suite cannot show, because the reference has a
session and s3rds does not: S3 is written once per commit and never by a look inside a transaction, a
conflicting writer fails the transaction instead of being overwritten, an old server is refused safely."""
import base64
import json

import pytest

import lambda_function as lf
from s3rds import sqlite

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()


def _handler(path, payload):
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": path,
             "headers": {"authorization": AUTH}, "body": json.dumps(payload)}
    resp = lf.lambda_handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def connect(fake_s3, monkeypatch, bucket):
    """connect(autocommit=...) -> an s3rds.sqlite connection served in-process; all share one database."""
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)

    def make(autocommit=True):
        c = sqlite.connect(host="in-process.test", user="admin", password="pw", database="d",
                           autocommit=autocommit)

        def post(path, payload):
            status, data = _handler(path, payload)
            if status >= 400:
                raise sqlite._error(status, data.get("error", ""))
            return data

        monkeypatch.setattr(c, "_post", post)
        c.create_database()
        return c

    first = make()
    first.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT UNIQUE) STRICT")
    return make


def values(conn):
    return [r["v"] for r in conn.query("SELECT v FROM t ORDER BY id")]


def test_reads_see_own_writes_others_see_them_after_commit(connect, fake_s3):
    tx, other = connect(autocommit=False), connect()
    puts = fake_s3.puts
    tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])
    assert tx.in_transaction
    assert values(tx) == ["a"]           # inside: the transaction's own write
    assert values(other) == []           # outside: nothing committed
    assert fake_s3.puts == puts          # a look inside a transaction never writes S3
    tx.commit()
    assert not tx.in_transaction
    assert values(other) == ["a"]
    assert fake_s3.puts == puts + 1      # one commit, one upload


def test_rollback_discards_without_touching_s3(connect, fake_s3):
    tx, other = connect(autocommit=False), connect()
    puts = fake_s3.puts
    tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])
    tx.execute("UPDATE t SET v = ?", ["b"])
    tx.rollback()
    tx.commit()                          # nothing left to commit
    assert values(other) == [] and fake_s3.puts == puts


def test_another_writer_makes_the_transaction_fail_not_overwrite(connect):
    tx, other = connect(autocommit=False), connect()
    tx.execute("INSERT INTO t (v) VALUES (?)", ["mine"])
    other.execute("INSERT INTO t (v) VALUES (?)", ["theirs"])
    with pytest.raises(sqlite.OperationalError, match="transaction conflict"):
        tx.execute("SELECT v FROM t")    # the next statement already sees the base is gone
    with pytest.raises(sqlite.OperationalError, match="transaction conflict"):
        tx.commit()                      # and so does commit(); nothing lands
    assert values(other) == ["theirs"]
    tx.execute("INSERT INTO t (v) VALUES (?)", ["mine"])   # run the transaction again
    tx.commit()
    assert values(other) == ["theirs", "mine"]


def test_a_failed_statement_leaves_the_transaction_usable(connect):
    tx, other = connect(autocommit=False), connect()
    tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])
    with pytest.raises(sqlite.IntegrityError):
        tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])    # UNIQUE; as sqlite3, only this statement fails
    tx.execute("INSERT INTO t (v) VALUES (?)", ["b"])
    tx.commit()
    assert values(other) == ["a", "b"]


def test_savepoint_and_transaction_statements(connect):
    tx, other = connect(autocommit=False), connect()
    tx.execute("BEGIN")
    tx.execute("INSERT INTO t (v) VALUES (?)", ["outer"])
    tx.execute("SAVEPOINT s1")
    tx.execute("INSERT INTO t (v) VALUES (?)", ["inner"])
    tx.execute("ROLLBACK TO SAVEPOINT s1")              # a statement, not the transaction's rollback
    assert values(tx) == ["outer"]
    tx.execute("COMMIT")
    assert values(other) == ["outer"]
    tx.execute("INSERT INTO t (v) VALUES (?)", ["gone"])
    tx.execute("ROLLBACK")
    assert values(other) == ["outer"]


def test_executemany_joins_the_transaction_and_lastrowid_survives_commit(connect):
    tx, other = connect(autocommit=False), connect()
    with tx.cursor() as cur:
        cur.executemany("INSERT INTO t (v) VALUES (?)", [["a"], ["b"], ["c"]])
        assert cur.rowcount == 3
        cur.execute("INSERT INTO t (v) VALUES (?)", ["d"])
        last = cur.lastrowid
    assert values(other) == []
    tx.commit()                                          # the replay gives the same ids
    assert other.query("SELECT id FROM t WHERE v = 'd'") == [{"id": last}]


def test_ddl_and_reads_outside_a_transaction_run_on_their_own(connect, fake_s3):
    tx, other = connect(autocommit=False), connect()
    tx.execute("CREATE TABLE u (id INTEGER PRIMARY KEY)")  # sqlite3: DDL does not open a transaction
    assert not tx.in_transaction
    assert other.query("SELECT name FROM sqlite_master WHERE name = 'u'") == [{"name": "u"}]
    puts = fake_s3.puts
    tx.execute("PRAGMA table_info(t)")                   # reflection reads never upload
    assert fake_s3.puts == puts and not tx.in_transaction


def test_isolation_level_none_commits_the_open_transaction(connect):
    tx, other = connect(autocommit=False), connect()
    assert tx.isolation_level == ""
    tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])
    tx.isolation_level = None                            # as sqlite3
    assert values(other) == ["a"] and tx.isolation_level is None


def test_errors_are_the_dbapi_classes(connect):
    c = connect()
    with pytest.raises(sqlite.IntegrityError):
        c.execute("INSERT INTO t (id, v) VALUES (1, 'x'), (2, 'x')")
    with pytest.raises(sqlite.OperationalError):
        c.execute("SELEC 1")
    with pytest.raises(sqlite.NotSupportedError):
        c.create_function("f", 1, abs)


def test_an_old_server_refuses_instead_of_committing(connect, monkeypatch):
    tx = connect(autocommit=False)
    monkeypatch.setattr(lf, "ROUTES", [r for r in lf.ROUTES if r[2] is not lf.replay])
    with pytest.raises(sqlite.OperationalError, match="redeploy the server"):
        tx.execute("INSERT INTO t (v) VALUES (?)", ["a"])


# ---- the server's /replay route ----

def test_replay_route_never_uploads_and_checks_the_base(connect, fake_s3):
    connect()
    puts = fake_s3.puts
    stmts = [{"sql": "INSERT INTO t (v) VALUES ('a')"}, {"sql": "SELECT COUNT(*) FROM t"}]
    status, data = _handler("/api/db/d/replay", {"statements": stmts, "replay": 1, "commit": False})
    assert status == 200 and data["results"][0]["rows"] == [[1]] and fake_s3.puts == puts
    status, data = _handler("/api/db/d/replay", {"statements": stmts, "replay": 0, "commit": False,
                                                 "base": '"not-the-current-etag"'})
    assert status == 409 and "transaction conflict" in data["error"]


def test_commit_inside_a_look_is_refused_and_the_cache_dropped(connect, fake_s3):
    c = connect()
    status, data = _handler("/api/db/d/replay", {"commit": False, "statements": [
        {"sql": "INSERT INTO t (v) VALUES ('ghost')"}, {"sql": "COMMIT"}]})
    assert status == 400 and "COMMIT" in data["error"]
    assert values(c) == []               # the local copy that saw the COMMIT is not served


# ---- SQLAlchemy ----

def test_sqlalchemy_url_and_conflict(connect, monkeypatch):
    sa = pytest.importorskip("sqlalchemy")
    from sqlalchemy.dialects import registry
    registry.register("sqlite.s3rds", "s3rds.sqlite", "SQLAlchemyDialect")
    url = sa.engine.make_url("sqlite+s3rds://admin:p%40ss@abc.lambda-url.us-east-2.on.aws/blog")
    dialect = url.get_dialect()()
    assert dialect.create_connect_args(url) == ([], {
        "host": "abc.lambda-url.us-east-2.on.aws", "user": "admin", "password": "p@ss",
        "database": "blog", "autocommit": False})

    engine = sa.create_engine("sqlite+s3rds://admin:pw@in-process.test/d",
                              creator=lambda: connect(autocommit=False))
    other = connect()
    with pytest.raises(sa.exc.OperationalError, match="transaction conflict"):
        with engine.begin() as c:
            c.execute(sa.text("INSERT INTO t (v) VALUES ('mine')"))
            other.execute("INSERT INTO t (v) VALUES ('theirs')")
    assert values(other) == ["theirs"]


def test_the_sqlalchemy_entry_point_is_declared():
    import os
    with open(os.path.join(os.path.dirname(__file__), "..", "client", "pyproject.toml"), encoding="utf-8") as f:
        text = f.read()
    assert '"sqlite.s3rds" = "s3rds.sqlite:SQLAlchemyDialect"' in text
