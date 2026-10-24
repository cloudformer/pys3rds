"""存储引擎的正确性承诺,每条一个测试。"""
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
    """模拟"别人"在 S3 上写了新版本:拿当前文件改一下再放回去。"""
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


# ---- 只有真写了才上传 ----

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


# ---- 原子性:失败就回到执行前 ----

def test_failed_statement_rolls_back_and_skips_upload(fake_s3, bucket):
    db = make(bucket, fake_s3)
    puts = fake_s3.puts

    def fn(c):
        c.execute("INSERT INTO t (v) VALUES ('ok')")
        c.execute("INSERT INTO t (v) VALUES (NULL)")  # NOT NULL 违约

    with pytest.raises(sqlite3.IntegrityError):
        db.run(fn)
    assert fake_s3.puts == puts            # S3 没动
    assert count(db) == 0                  # 第一条也回滚了


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


# ---- 并发写:CAS 重放,绝不覆盖别人 ----

def test_conflict_replays_on_fresh_copy(fake_s3, bucket):
    db = make(bucket, fake_s3)
    calls = []
    real_put = fake_s3.put_object

    def put_with_intruder(**kw):
        if not calls:  # 第一次上传前,别人先写了
            calls.append("intruder")
            other_writer_bytes(fake_s3, bucket, "theirs")
        return real_put(**kw)

    fake_s3.put_object = put_with_intruder
    runs = []
    db.run(lambda c: (runs.append(1), c.execute("INSERT INTO t (v) VALUES ('mine')")))
    assert len(runs) == 2  # fn 被重放了一次
    values = sorted(db.read(lambda c: [r[0] for r in c.execute("SELECT v FROM t")]))
    assert values == ["mine", "theirs"]  # 两边的写都在


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
    assert "mine" not in values  # 我的写入干净地没发生,读到的是 S3 真相


# ---- 上传失败不留幽灵数据 ----

def test_upload_failure_invalidates_cache(fake_s3, bucket):
    db = make(bucket, fake_s3)

    def broken_put(**kw):
        raise ClientError({"Error": {"Code": "InternalError", "Message": ""}}, "PutObject")

    real_put = fake_s3.put_object
    fake_s3.put_object = broken_put
    with pytest.raises(ClientError):
        db.run(lambda c: c.execute("INSERT INTO t (v) VALUES ('ghost')"))
    fake_s3.put_object = real_put
    assert count(db) == 0  # 本地那份带 ghost 的副本被作废,重新拉了 S3 的


# ---- 下载时清掉残留日志 ----

def test_download_removes_stale_journal(fake_s3, bucket):
    db = make(bucket, fake_s3)
    journal = db.local + "-journal"
    with open(journal, "wb") as f:
        f.write(b"stale hot journal")
    SqliteS3._etags.clear()  # 模拟进程重启
    assert count(SqliteS3(bucket, KEY)) == 0
    assert not os.path.exists(journal)


# ---- 只读连接真的只读 ----

def test_read_refuses_writes(fake_s3, bucket):
    db = make(bucket, fake_s3)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        db.read(lambda c: c.execute("INSERT INTO t (v) VALUES ('x')"))


# ---- 外键生效 ----

def test_foreign_keys_enforced(fake_s3, bucket):
    db = make(bucket, fake_s3)
    db.run(lambda c: c.execute(
        "CREATE TABLE child (id INTEGER PRIMARY KEY, t_id INTEGER REFERENCES t(id)) STRICT"))
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        db.run(lambda c: c.execute("INSERT INTO child (t_id) VALUES (999)"))


# ---- 缓存命中不重复下载 ----

def test_cache_hit_skips_download(fake_s3, bucket):
    db = make(bucket, fake_s3)
    count(db)
    assert db.stats["downloaded"] is False
    other_writer_bytes(fake_s3, bucket, "x")
    assert count(db) == 1
    assert db.stats["downloaded"] is True  # S3 变了才重拉


def test_create_database_refuses_overwrite(fake_s3, bucket):
    create_database(bucket, KEY)
    with pytest.raises(s3_sqlite.DatabaseExists):
        create_database(bucket, KEY)
