"""SQLite on S3: one .sqlite object on S3 used as one database instance.

read(fn):  opens the local cache mode=ro, SQLite itself refuses writes; never uploads.
run(fn):   after fn, SQLite's authorizer says whether data really changed; only then the whole file
           goes back with If-Match (CAS); on 412 re-download, replay, retry; out of retries -> WriteConflict.
           Rule: failing is allowed, duplicating a write or overwriting someone else's is not.
Cache:     /tmp keyed by ETag; downloads go through a temp file + os.replace, never a half file.
One key = one database; the lock is the file, so different databases never conflict.

Tests swap the module attribute s3 for a fake client (s3_sqlite.s3 = Fake()).
"""
import hashlib
import os
import shutil

try:
    import sqlean as sqlite3   # the pinned engine (src/db/requirements.txt), bundled into the Lambda zip
except ImportError:            # from source without it: the local regression tests only
    import sqlite3
if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") and sqlite3.__name__ != "sqlean":
    raise RuntimeError("the zip lacks the pinned SQLite engine: build it with python3 scripts/build_server.py")

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

_NOT_FOUND = ("404", "NoSuchKey", "NotFound")
_CONFLICT = ("PreconditionFailed", "ConditionalRequestConflict")
_TMP_MIN_FREE = 64 * 1024 * 1024  # below this much free /tmp, evict the least recently used caches
# authorizer actions that do not change data; PRAGMA is special (an assignment counts as a write); everything else is a write
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT,
}
# PRAGMAs whose argument names a table / index (PRAGMA table_info("t")): reads, not assignments.
# ORMs reflect with them on every metadata call; counted as writes each one re-uploaded the database
_READ_PRAGMAS = {
    "table_info", "table_xinfo", "table_list", "index_list", "index_info", "index_xinfo",
    "foreign_key_list", "foreign_key_check", "integrity_check", "quick_check",
}


# connection settings a request may carry (the driver keeps them per connection, as sqlite3 does); applied
# before BEGIN, where they take effect. Foreign keys are on unless the request turns them off
SESSION_PRAGMAS = {"foreign_keys": 1, "legacy_alter_table": 0}


def _apply_pragmas(conn, pragmas):
    for name, value in {**SESSION_PRAGMAS, **(pragmas or {})}.items():
        if name in SESSION_PRAGMAS:  # names and 0 / 1 values are checked by the HTTP layer; never interpolate others
            conn.execute(f"PRAGMA {name}={int(bool(value))}")


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
    """A cheap fact about whether the file changed: any SQLite write updates mtime."""
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def list_databases(bucket, prefix):
    """List every .sqlite under prefix; returns the names without prefix and extension."""
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
    """Create an empty database. If-None-Match raises DatabaseExists instead of overwriting."""
    tmp = _local_path(bucket, key) + ".new"
    conn = sqlite3.connect(tmp)
    conn.execute("PRAGMA user_version = 1")  # forces a valid SQLite header onto disk
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
    """When /tmp runs low, delete the least recently updated half of the cached databases (not the current one)."""
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
    """One instance = one database (one S3 key).

    Usage:
        db = SqliteS3(bucket, "db/blog.sqlite")
        rows = db.read(lambda c: c.execute("SELECT ...").fetchall())
        db.run(lambda c: c.execute("INSERT ..."))   # uploads only if data changed
    """

    # shared within a warm Lambda instance: /tmp path -> the S3 ETag it holds
    _etags = {}

    def __init__(self, bucket, key, max_retries=3):
        self.bucket = bucket
        self.key = key
        self.max_retries = max_retries
        self.local = _local_path(bucket, key)
        # what the last read/run did: whether S3 was actually touched
        self.stats = {"downloaded": False, "uploaded": False}

    # ---- cache ----

    def _download(self):
        _evict_tmp(keep=self.local)
        resp = s3.get_object(Bucket=self.bucket, Key=self.key)
        tmp = self.local + ".tmp"
        with open(tmp, "wb") as f:
            f.write(resp["Body"].read())
        # a run killed mid-commit leaves a hot journal; a new file next to an old journal would be
        # "rolled back" into a corrupt database, so side files go first, then the main file atomically
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                os.remove(self.local + suffix)
            except FileNotFoundError:
                pass
        os.replace(tmp, self.local)  # atomic: a kill in the middle never leaves half a file
        SqliteS3._etags[self.local] = resp["ETag"]
        self.stats["downloaded"] = True

    def _invalidate(self):
        """Call when the local copy cannot be trusted: drop the cache, force a re-download next time."""
        SqliteS3._etags.pop(self.local, None)

    @property
    def etag(self):
        """The S3 version (ETag) of the current local copy; valid after read/run."""
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

    # ---- read ----

    def read(self, fn, detect_types=0, pragmas=None):
        """fn(conn) -> result. The connection is read-only; SQLite refuses any write.
        With detect_types=sqlite3.PARSE_DECLTYPES the registered converters run by declared column type (when the driver asks for types)."""
        self._reset_stats()
        self._ensure_fresh()
        conn = sqlite3.connect(f"file:{self.local}?mode=ro", uri=True, detect_types=detect_types)
        conn.row_factory = sqlite3.Row
        try:
            _apply_pragmas(conn, pragmas)
            return fn(conn)
        finally:
            conn.close()

    # ---- read / write ----

    def run(self, fn, detect_types=0, pragmas=None):
        """fn(conn) -> result. Uploads with If-Match only if fn really changed data.

        fn must be replayable (plain SQL is): on a 412 conflict it runs again on the freshly downloaded file.
        When every retry conflicts, WriteConflict is raised: the local change is dropped, S3 keeps the other writer's version.
        """
        self._reset_stats()
        last_err = None
        for _ in range(self.max_retries):
            self._ensure_fresh()
            etag = SqliteS3._etags[self.local]
            before = _fingerprint(self.local)
            result, dirty = self._transact(fn, detect_types, pragmas=pragmas)
            # the authorizer sees intent, the file fingerprint sees facts (catches PRAGMA optimize and friends that write without an assignment)
            if not dirty and _fingerprint(self.local) == before:
                return result

            # from here the local file is a version S3 does not have: any failure must drop the cache,
            # or the next request would treat it as fresh, read ghost data and even upload it later
            try:
                with open(self.local, "rb") as f:
                    body = f.read()
                resp = s3.put_object(
                    Bucket=self.bucket, Key=self.key, Body=body, IfMatch=etag)
            except ClientError as e:
                self._invalidate()
                if _error_code(e) in _CONFLICT:
                    last_err = e
                    continue  # next round: re-download, replay fn
                raise
            except BaseException:
                self._invalidate()
                raise
            SqliteS3._etags[self.local] = resp["ETag"]
            self.stats["uploaded"] = True
            return result

        raise WriteConflict(
            f"s3://{self.bucket}/{self.key}: write conflict, "
            f"gave up after {self.max_retries} retries; this write did not land") from last_err

    # ---- write, look, roll back ----

    def trial(self, fn, detect_types=0, pragmas=None):
        """fn(conn) inside a transaction that is always rolled back: fn sees its own writes, nobody else
        does, S3 is never written. The driver's transactions are built on it: replay the statements written
        so far, run one more, return its result, forget everything."""
        self._reset_stats()
        self._ensure_fresh()
        result, _dirty = self._transact(fn, detect_types, commit=False, pragmas=pragmas)
        return result

    def _transact(self, fn, detect_types=0, commit=True, pragmas=None):
        """Run fn inside an explicit transaction (DDL included); returns (result, whether the authorizer saw a write).

        fn raising -> ROLLBACK, the local file is as before; only a failed ROLLBACK drops the cache.
        commit=False -> ROLLBACK even when fn succeeds. pragmas: the request's connection settings (SESSION_PRAGMAS).
        """
        dirty = False

        def authorizer(action, arg1, arg2, _dbname, _source):  # signature fixed by sqlite3
            nonlocal dirty
            if action == sqlite3.SQLITE_PRAGMA:
                # only PRAGMA x = v is a write; the file fingerprint still catches anything that changed the file
                if arg2 is not None and (arg1 or "").lower() not in _READ_PRAGMAS:
                    dirty = True
            elif action not in _READ_ACTIONS:
                dirty = True
            return sqlite3.SQLITE_OK

        # isolation_level=None: no implicit transactions from the sqlite3 module; we BEGIN/COMMIT ourselves
        conn = sqlite3.connect(self.local, isolation_level=None, detect_types=detect_types)
        conn.row_factory = sqlite3.Row
        try:
            # if the database is in WAL mode switch back to a single-file journal so the main file is complete after close (outside the transaction)
            conn.execute("PRAGMA journal_mode=DELETE")
            _apply_pragmas(conn, pragmas)  # foreign keys on unless the request turned them off (SQLite's default is off)
            conn.set_authorizer(authorizer)
            conn.execute("BEGIN")
            try:
                result = fn(conn)
                if not commit and not conn.in_transaction:
                    # a COMMIT / END inside a trial wrote to the local file only: that copy is no longer S3's
                    self._invalidate()
                    raise sqlite3.OperationalError("COMMIT / END inside a transaction trial; use the commit request")
                if conn.in_transaction:  # fn may have committed itself
                    conn.execute("COMMIT" if commit else "ROLLBACK")
            except BaseException:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except sqlite3.Error:
                    self._invalidate()  # even ROLLBACK failed: the local state cannot be trusted
                raise
        finally:
            conn.close()
        return result, dirty
