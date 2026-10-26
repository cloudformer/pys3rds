"""The storage engine's guarantees, one test each."""
import os
import sqlite3

import pytest
from botocore.exceptions import ClientError

import s3_sqlite
from s3_sqlite import SqliteS3, WriteConflict, create_database

KEY = "db/t.sqlite"
SCHEMA = "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL) STRICT"


def make(bucket, fake_s3):
    create_database(bucket, KEY)
    db = SqliteS3(bucket, KEY)
    db.run(lambda c: c.execute(SCHEMA))
    return db


def count(db):
    return db.read(lambda c: c.execute("SELECT COUNT(*) FROM t").fetchone()[0])


def other_writer_bytes(fake_s3, bucket, extra_value):
    """Simulate another writer: take the current file, change it, put it back."""
    path = "/tmp/other-writer.sqlite"
    with open(path, "wb") as f:
        f.write(fake_s3.objects[KEY])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO t (v) VALUES (?)", (extra_value,))
    conn.commit()
    conn.close()
    with open(path, "rb") as f:
        fake_s3.objects[KEY] = f.read()
    os.remove(path)


# ---- upload only when data really changed ----

def test_select_does_not_upload(fake_s3, bucket):
    db = make(bucket, fake_s3)
    puts = fake_s3.puts
    rows = db.run(lambda c: c.execute("SELECT 1").fetchall())
    assert rows[0][0] == 1
    assert fake_s3.puts == puts
    assert db.stats["uploaded"] is False


def test_insert_uploads_once(fake_s3, bucket):
    db = make(bucket, fake_s3)
    puts = fake_s3.puts
    db.run(lambda c: c.execute("INSERT INTO t (v) VALUES ('a')"))
    assert fake_s3.puts == puts + 1
    assert db.stats["uploaded"] is True
    assert count(db) == 1


# ---- atomicity: a failure leaves everything as before ----

def test_failed_statement_rolls_back_and_skips_upload(fake_s3, bucket):
    db = make(bucket, fake_s3)
    puts = fake_s3.puts

    def fn(c):
        c.execute("INSERT INTO t (v) VALUES ('ok')")
        c.execute("INSERT INTO t (v) VALUES (NULL)")  # violates NOT NULL

    with pytest.raises(sqlite3.IntegrityError):
        db.run(fn)
    assert fake_s3.puts == puts            # S3 untouched
    assert count(db) == 0                  # the first statement rolled back too


def test_ddl_is_transactional(fake_s3, bucket):
    db = make(bucket, fake_s3)

    def fn(c):
        c.execute("CREATE TABLE u (x INTEGER) STRICT")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        db.run(fn)
    tables = db.read(lambda c: [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    assert "u" not in tables


# ---- concurrent writes: CAS replay, never overwrite the other writer ----

def test_conflict_replays_on_fresh_copy(fake_s3, bucket):
    db = make(bucket, fake_s3)
    calls = []
    real_put = fake_s3.put_object

    def put_with_intruder(**kw):
        if not calls:  # someone else wrote before our first upload
            calls.append("intruder")
            other_writer_bytes(fake_s3, bucket, "theirs")
        return real_put(**kw)

    fake_s3.put_object = put_with_intruder
    runs = []
    db.run(lambda c: (runs.append(1), c.execute("INSERT INTO t (v) VALUES ('mine')")))
    assert len(runs) == 2  # fn was replayed once
    values = sorted(db.read(lambda c: [r[0] for r in c.execute("SELECT v FROM t")]))
    assert values == ["mine", "theirs"]  # both writes survived


def test_conflict_exhausted_fails_clean(fake_s3, bucket):
    db = make(bucket, fake_s3)
    real_put = fake_s3.put_object

    def always_conflict(**kw):
        other_writer_bytes(fake_s3, bucket, "theirs")
        return real_put(**kw)

    fake_s3.put_object = always_conflict
    with pytest.raises(WriteConflict):
        db.run(lambda c: c.execute("INSERT INTO t (v) VALUES ('mine')"))
    fake_s3.put_object = real_put
    values = db.read(lambda c: [r[0] for r in c.execute("SELECT v FROM t")])
    assert "mine" not in values  # my write cleanly did not happen; what we read is S3's truth


# ---- a failed upload leaves no ghost data ----

def test_upload_failure_invalidates_cache(fake_s3, bucket):
    db = make(bucket, fake_s3)

    def broken_put(**kw):
        raise ClientError({"Error": {"Code": "InternalError", "Message": ""}}, "PutObject")

    real_put = fake_s3.put_object
    fake_s3.put_object = broken_put
    with pytest.raises(ClientError):
        db.run(lambda c: c.execute("INSERT INTO t (v) VALUES ('ghost')"))
    fake_s3.put_object = real_put
    assert count(db) == 0  # the local copy with the ghost row was dropped and S3's re-downloaded


# ---- stale journals are removed on download ----

def test_download_removes_stale_journal(fake_s3, bucket):
    db = make(bucket, fake_s3)
    journal = db.local + "-journal"
    with open(journal, "wb") as f:
        f.write(b"stale hot journal")
    SqliteS3._etags.clear()  # simulate a process restart
    assert count(SqliteS3(bucket, KEY)) == 0
    assert not os.path.exists(journal)


# ---- the read-only connection is really read-only ----

def test_read_refuses_writes(fake_s3, bucket):
    db = make(bucket, fake_s3)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.read(lambda c: c.execute("INSERT INTO t (v) VALUES ('x')"))


# ---- foreign keys are enforced ----

def test_foreign_keys_enforced(fake_s3, bucket):
    db = make(bucket, fake_s3)
    db.run(lambda c: c.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, t_id INTEGER REFERENCES t(id)) STRICT"))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.run(lambda c: c.execute("INSERT INTO child (t_id) VALUES (999)"))


# ---- a cache hit does not download again ----

def test_cache_hit_skips_download(fake_s3, bucket):
    db = make(bucket, fake_s3)
    count(db)
    assert db.stats["downloaded"] is False
    other_writer_bytes(fake_s3, bucket, "x")
    assert count(db) == 1
    assert db.stats["downloaded"] is True  # re-downloaded only because S3 changed


def test_create_database_refuses_overwrite(fake_s3, bucket):
    create_database(bucket, KEY)
    with pytest.raises(s3_sqlite.DatabaseExists):
        create_database(bucket, KEY)
