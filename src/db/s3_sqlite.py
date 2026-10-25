"""SQLite-on-S3:把 S3 上的一个 .sqlite 文件当作一个数据库实例来用。

read(fn):  以 mode=ro 打开本地缓存,SQLite 自己保证写不进去;永不回传。
run(fn):   执行后由 SQLite authorizer 判定是否真的改了数据——改了才整文件
           带 If-Match 回传(CAS);412 冲突则重拉、重放、重试;重试耗尽抛 WriteConflict。
           原则:可以失败,绝不重复写、绝不覆盖别人的写。
缓存:      /tmp 按 ETag 热缓存;下载经临时文件 + os.replace,不会留下半个文件。
一个 key 一个库,锁粒度是文件,不同库之间永远不冲突。

测试时把模块属性 s3 换成假客户端即可(s3_sqlite.s3 = Fake())。
"""
import hashlib
import os
import shutil
import sqlite3

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

_NOT_FOUND = ("404", "NoSuchKey", "NotFound")
_CONFLICT = ("PreconditionFailed", "ConditionalRequestConflict")
_TMP_MIN_FREE = 64 * 1024 * 1024  # /tmp 剩余低于此值就淘汰最久未用的缓存
# authorizer 里"不改数据"的动作;PRAGMA 单独判(带赋值才算写);其余一律算写
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
}


class DatabaseNotFound(Exception):
    pass


class DatabaseExists(Exception):
    pass


class WriteConflict(Exception):
    pass


def _local_path(bucket, key):
    digest = hashlib.sha1(f"{bucket}/{key}".encode()).hexdigest()
    return f"/tmp/{digest}.sqlite"


def _error_code(e):
    return e.response.get("Error", {}).get("Code", "")


def _fingerprint(path):
    """文件是否被改过的廉价事实判据:SQLite 任何落盘都会改 mtime。"""
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def list_databases(bucket, prefix):
    """列出 prefix 下所有 .sqlite,返回去掉前缀和扩展名的库名。"""
    names = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".sqlite"):
                names.append(key[len(prefix):-len(".sqlite")])
    return sorted(names)


def database_exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if _error_code(e) in _NOT_FOUND:
            return False
        raise


def create_database(bucket, key):
    """新建空库。If-None-Match 保证已存在时抛 DatabaseExists 而不是覆盖。"""
    tmp = _local_path(bucket, key) + ".new"
    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA user_version = 1")  # 让文件落盘成合法的 SQLite 头
    conn.commit()
    conn.close()
    try:
        with open(tmp, "rb") as f:
            s3.put_object(Bucket=bucket, Key=key, Body=f.read(), IfNoneMatch="*")
    except ClientError as e:
        if _error_code(e) in _CONFLICT:
            raise DatabaseExists(key) from None
        raise
    finally:
        os.remove(tmp)


def _evict_tmp(keep):
    """/tmp 空间紧张时删掉最久未更新的一半缓存库(当前库除外)。"""
    try:
        if shutil.disk_usage("/tmp").free > _TMP_MIN_FREE:
            return
        files = [os.path.join("/tmp", f) for f in os.listdir("/tmp")
                 if f.endswith(".sqlite") and os.path.join("/tmp", f) != keep]
        files.sort(key=os.path.getmtime)
        for f in files[:max(1, len(files) // 2)]:
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(f + suffix)
                except FileNotFoundError:
                    pass
            SqliteS3._etags.pop(f, None)
    except OSError:
        pass


class SqliteS3:
    """一个实例对应一个库(一个 S3 key)。

    用法:
        db = SqliteS3(bucket, "db/blog.sqlite")
        rows = db.read(lambda c: c.execute("SELECT ...").fetchall())
        db.run(lambda c: c.execute("INSERT ..."))   # 真写了才回传 S3
    """

    # 热 Lambda 实例内共享:/tmp 文件路径 -> 它对应的 S3 ETag
    _etags = {}

    def __init__(self, bucket, key, max_retries=3):
        self.bucket = bucket
        self.key = key
        self.max_retries = max_retries
        self.local = _local_path(bucket, key)
        # 最近一次 read/run 的性能感知:有没有真的去 S3 搬文件
        self.stats = {"downloaded": False, "uploaded": False}

    # ---- 缓存 ----

    def _download(self):
        _evict_tmp(keep=self.local)
        resp = s3.get_object(Bucket=self.bucket, Key=self.key)
        tmp = self.local + ".tmp"
        with open(tmp, "wb") as f:
            f.write(resp["Body"].read())
        # 上次执行若在 commit 中途被杀,会留下热 journal;新文件配旧 journal 会被 SQLite
        # "回滚"成坏库,所以侧文件必须先清掉,再原子换上主文件
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.remove(self.local + suffix)
            except FileNotFoundError:
                pass
        os.replace(tmp, self.local)  # 原子替换,中途被杀不会留下半个文件
        SqliteS3._etags[self.local] = resp["ETag"]
        self.stats["downloaded"] = True

    def _invalidate(self):
        """本地副本状态不可信时调用:作废缓存,下次强制重拉。"""
        SqliteS3._etags.pop(self.local, None)

    @property
    def etag(self):
        """当前本地副本对应的 S3 版本(ETag);read/run 之后有效。"""
        return SqliteS3._etags.get(self.local)

    def _ensure_fresh(self):
        try:
            head = s3.head_object(Bucket=self.bucket, Key=self.key)
        except ClientError as e:
            if _error_code(e) in _NOT_FOUND:
                raise DatabaseNotFound(f"s3://{self.bucket}/{self.key}") from None
            raise
        if SqliteS3._etags.get(self.local) != head["ETag"] or not os.path.exists(self.local):
            self._download()

    def _reset_stats(self):
        self.stats = {"downloaded": False, "uploaded": False}

    # ---- 读 ----

    def read(self, fn, detect_types=0):
        """fn(conn) -> result。连接以只读模式打开,任何写都会被 SQLite 拒绝。
        detect_types=sqlite3.PARSE_DECLTYPES 时按列的声明类型调用已注册的 converter(驱动要类型信息时用)。"""
        self._reset_stats()
        self._ensure_fresh()
        conn = sqlite3.connect(f"file:{self.local}?mode=ro", uri=True, detect_types=detect_types)
        conn.row_factory = sqlite3.Row
        try:
            return fn(conn)
        finally:
            conn.close()

    # ---- 读写 ----

    def run(self, fn, detect_types=0):
        """fn(conn) -> result。只有 fn 真的改了数据才带 If-Match 回传 S3。

        fn 必须可重放(纯 SQL 即可):412 冲突时会在新下载的文件上重新执行。
        所有重试都冲突时抛 WriteConflict——本地改动被丢弃,S3 上保持别人的版本。
        """
        self._reset_stats()
        last_err = None
        for _ in range(self.max_retries):
            self._ensure_fresh()
            etag = SqliteS3._etags[self.local]
            before = _fingerprint(self.local)
            result, dirty = self._transact(fn, detect_types)
            # authorizer 管"意图",文件指纹管"事实"(兜住 PRAGMA optimize 这类不带赋值却写文件的)
            if not dirty and _fingerprint(self.local) == before:
                return result

            # 从这里起本地文件已经是"S3 上没有的新版本":任何失败都必须作废缓存,
            # 否则下次请求会把它当新鲜缓存用,读到幽灵数据甚至在之后的写里悄悄带上去
            try:
                with open(self.local, "rb") as f:
                    body = f.read()
                resp = s3.put_object(
                    Bucket=self.bucket, Key=self.key, Body=body, IfMatch=etag)
            except ClientError as e:
                self._invalidate()
                if _error_code(e) in _CONFLICT:
                    last_err = e
                    continue  # 下一轮重拉最新文件、重放 fn
                raise
            except BaseException:
                self._invalidate()
                raise
            SqliteS3._etags[self.local] = resp["ETag"]
            self.stats["uploaded"] = True
            return result

        raise WriteConflict(
            f"s3://{self.bucket}/{self.key}: 并发写冲突,"
            f"{self.max_retries} 次重试后放弃,本次写入未生效") from last_err

    def _transact(self, fn, detect_types=0):
        """在显式事务里执行 fn(DDL 也进事务);返回 (result, authorizer 是否看到写动作)。

        fn 抛异常 → ROLLBACK,本地文件回到执行前;ROLLBACK 自己都失败才作废缓存。
        """
        dirty = False

        def authorizer(action, _arg1, arg2, _dbname, _source):  # 签名由 sqlite3 规定
            nonlocal dirty
            if action == sqlite3.SQLITE_PRAGMA:
                if arg2 is not None:  # PRAGMA x = v 才是写
                    dirty = True
            elif action not in _READ_ACTIONS:
                dirty = True
            return sqlite3.SQLITE_OK

        # isolation_level=None:关掉 sqlite3 模块的隐式事务,自己 BEGIN/COMMIT
        conn = sqlite3.connect(self.local, isolation_level=None, detect_types=detect_types)
        conn.row_factory = sqlite3.Row
        try:
            # 万一库是 WAL 模式,改回单文件日志,保证 close 后主文件自洽(需在事务外)
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA foreign_keys=ON")  # SQLite 默认不查外键,这里强制开
            conn.set_authorizer(authorizer)
            conn.execute("BEGIN")
            try:
                result = fn(conn)
                if conn.in_transaction:  # fn 自己 COMMIT 过就不重复
                    conn.execute("COMMIT")
            except BaseException:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    self._invalidate()  # 回滚都失败,本地状态不可信
                raise
        finally:
            conn.close()
        return result, dirty
