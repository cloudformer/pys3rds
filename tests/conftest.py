"""Test setup: client/ on the import path, an in-memory fake S3.

The fake S3 implements everything s3_sqlite uses: ETag (= MD5), HEAD 404,
conditional PUT with If-Match / If-None-Match, and the list paginator.
"""
import hashlib
import io
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "client"))

# lambda_function reads these at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("DB_BUCKET", "test-bucket")
os.environ.setdefault("DB_PREFIX", "db/")
os.environ.setdefault("DB_USER", "admin")
os.environ.setdefault("DB_PASSWORD", "pw")

from botocore.exceptions import ClientError  # noqa: E402

# compatibility report (tests/compat/REPORT.md): hooks must live in the root conftest to see every test
from compat_report import (pytest_collection_modifyitems, pytest_configure,  # noqa: E402,F401
                           pytest_runtest_logreport, pytest_sessionfinish, pytest_terminal_summary)

pytest_plugins = ["compat_docker", "compat_aws"]   # python3 -m pytest tests -q [--docker up | run | down] [--aws <var-file>]


class FakeS3:
    def __init__(self):
        self.objects = {}  # key -> bytes
        self.puts = 0

    @staticmethod
    def etag(body):
        return '"%s"' % hashlib.md5(body).hexdigest()

    @staticmethod
    def _err(code, op):
        raise ClientError({"Error": {"Code": code, "Message": code}}, op)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            self._err("404", "HeadObject")
        return {"ETag": self.etag(self.objects[Key])}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            self._err("NoSuchKey", "GetObject")
        body = self.objects[Key]
        return {"ETag": self.etag(body), "Body": io.BytesIO(body)}

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            self._err("PreconditionFailed", "PutObject")
        if IfMatch is not None and (
                Key not in self.objects or self.etag(self.objects[Key]) != IfMatch):
            self._err("PreconditionFailed", "PutObject")
        self.objects[Key] = bytes(Body)
        self.puts += 1
        return {"ETag": self.etag(Body)}

    def get_paginator(self, name):
        objects = self.objects

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(objects)
                                    if k.startswith(Prefix)]}
        return Paginator()


@pytest.fixture
def fake_s3(monkeypatch):
    from s3rds.server import s3_sqlite
    fake = FakeS3()
    monkeypatch.setattr(s3_sqlite, "s3", fake)
    s3_sqlite.SqliteS3._etags.clear()
    return fake


@pytest.fixture
def bucket():
    """A bucket name per test -> a different /tmp cache path, so tests never share state."""
    return f"t-{uuid.uuid4().hex}"
