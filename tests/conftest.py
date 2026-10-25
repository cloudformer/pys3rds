"""测试公共设施:把 src/ 加进 import 路径,提供一个内存版假 S3。

假 S3 实现了 s3_sqlite 用到的全部语义:ETag(=MD5)、HEAD 404、
PUT 的 If-Match / If-None-Match 条件写,以及 list 分页器。
"""
import hashlib
import io
import os
import sys
import uuid

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "db"))
sys.path.insert(0, os.path.join(ROOT, "client"))

# lambda_function 在 import 时就读这些环境变量
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("DB_BUCKET", "test-bucket")
os.environ.setdefault("DB_PREFIX", "db/")
os.environ.setdefault("DB_USER", "admin")
os.environ.setdefault("DB_PASSWORD", "pw")

from botocore.exceptions import ClientError  # noqa: E402

# compatibility report (tests/compat/REPORT.md): hooks must live in the root conftest to see every test
from compat_report import (pytest_collection_modifyitems, pytest_configure,  # noqa: E402,F401
                           pytest_runtest_logreport, pytest_sessionfinish, pytest_terminal_summary)


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
    import s3_sqlite
    fake = FakeS3()
    monkeypatch.setattr(s3_sqlite, "s3", fake)
    s3_sqlite.SqliteS3._etags.clear()
    return fake


@pytest.fixture
def bucket():
    """每个测试独立桶名 → /tmp 缓存路径不同,测试之间互不污染。"""
    return f"t-{uuid.uuid4().hex}"
