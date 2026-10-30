"""The server's HTTP layer: validation before anything else, atomic /tx, the read-only endpoint."""
import base64
import json
import os
import uuid

import pytest

from s3rds.server import lambda_function as lf
from s3rds.server import s3_sqlite

AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()


def call(method, path, body=None, auth=AUTH):
    event = {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"authorization": auth} if auth else {},
        "body": json.dumps(body) if body is not None else None,
    }
    resp = lf.lambda_handler(event, None)
    return resp["statusCode"], json.loads(resp["body"])


@pytest.fixture
def db(fake_s3, monkeypatch, bucket):
    monkeypatch.setattr(lf, "BUCKET", bucket)
    monkeypatch.setattr(lf, "READONLY", False)
    assert call("POST", "/api/databases", {"name": "d"})[0] == 201
    status, _ = call("POST", "/api/db/d/query",
                     {"sql": "CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT NOT NULL) STRICT"})
    assert status == 200
    return fake_s3


def test_console_on_and_off(monkeypatch):
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/", "headers": {}}
    monkeypatch.setattr(lf, "CONSOLE", True)
    on = lf.lambda_handler(event, None)
    assert on["statusCode"] == 200 and on["headers"]["content-type"].startswith("text/html")
    monkeypatch.setattr(lf, "CONSOLE", False)
    off = lf.lambda_handler(event, None)
    assert off["statusCode"] == 404 and "console" in json.loads(off["body"])["error"]


def test_bad_credentials(db):
    assert call("GET", "/api/databases", auth="Basic " + base64.b64encode(b"a:b").decode())[0] == 401


@pytest.mark.parametrize("user, password", [("", ""), ("admin", ""), ("", "pw")])
def test_no_credentials_configured_is_503(db, monkeypatch, user, password):
    monkeypatch.setattr(lf, "DB_USER", user)
    monkeypatch.setattr(lf, "DB_PASSWORD", password)
    monkeypatch.setattr(lf, "ALLOW_NO_AUTH", False)
    for auth in (AUTH, None, "Basic " + base64.b64encode(b"admin:").decode()):
        status, data = call("GET", "/api/databases", auth=auth)
        assert status == 503 and "no credentials configured" in data["error"]


def test_allow_no_auth_answers_with_a_warning(db, monkeypatch):
    monkeypatch.setattr(lf, "DB_USER", "")
    monkeypatch.setattr(lf, "DB_PASSWORD", "")
    monkeypatch.setattr(lf, "ALLOW_NO_AUTH", True)
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/databases", "headers": {}}
    resp = lf.lambda_handler(event, None)
    assert resp["statusCode"] == 200 and resp["headers"]["x-s3rds-warning"] == "no-auth"


def test_configured_credentials_carry_no_warning(db):
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/databases",
             "headers": {"authorization": AUTH}}
    assert "x-s3rds-warning" not in lf.lambda_handler(event, None)["headers"]


# ---------------- one request, one database file: ATTACH / DETACH / VACUUM INTO are refused ----------------

@pytest.fixture
def other_cache(db, bucket):
    """A second database e with a row, cached in /tmp: the file an ATTACH from d would aim at."""
    assert call("POST", "/api/databases", {"name": "e"})[0] == 201
    assert call("POST", "/api/db/e/query", {"sql": "CREATE TABLE s (v TEXT)"})[0] == 200
    assert call("POST", "/api/db/e/query", {"sql": "INSERT INTO s VALUES ('orig')"})[0] == 200
    path = s3_sqlite._local_path(bucket, "db/e.sqlite")
    assert os.path.exists(path)
    return path


def _content(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.mark.parametrize("readonly", [True, False], ids=["ro", "rw"])
def test_attach_another_cache_is_403(db, other_cache, monkeypatch, readonly):
    monkeypatch.setattr(lf, "READONLY", readonly)
    before = (s3_sqlite._fingerprint(other_cache), _content(other_cache))
    objects, puts = dict(db.objects), db.puts
    attach = {"sql": f"ATTACH DATABASE '{other_cache}' AS o"}
    write = {"sql": "INSERT INTO o.s VALUES ('tampered')"}
    requests = [
        ("/api/db/d/query", attach),
        ("/api/db/d/query", {"sql": "DETACH o"}),
        ("/api/db/d/tx", {"statements": [attach, write]}),
        ("/api/db/d/replay", {"statements": [attach, write], "commit": False}),
        ("/api/db/d/replay", {"statements": [attach, write], "commit": True}),
    ]
    for path, body in requests:
        status, data = call("POST", path, body)
        assert status == 403, (path, body, data)
        assert "ATTACH / DETACH / VACUUM INTO are not allowed" in data["error"]
    assert (s3_sqlite._fingerprint(other_cache), _content(other_cache)) == before  # the other cache is untouched
    assert db.puts == puts and db.objects == objects                               # S3: no new version of either
    _, data = call("POST", "/api/db/e/query", {"sql": "SELECT v FROM s"})
    assert data["rows"] == [["orig"]]


@pytest.mark.parametrize("readonly", [True, False], ids=["ro", "rw"])
def test_vacuum_into_is_403(db, monkeypatch, readonly):
    monkeypatch.setattr(lf, "READONLY", readonly)
    target = f"/tmp/s3rds-test-{uuid.uuid4().hex}.sqlite"
    puts = db.puts
    status, data = call("POST", "/api/db/d/query", {"sql": f"VACUUM INTO '{target}'"})
    assert status == 403 and "VACUUM INTO" in data["error"]
    assert not os.path.exists(target)
    assert db.puts == puts


def test_invalid_json_is_400_before_touching_data(db):
    event = {"requestContext": {"http": {"method": "POST"}}, "rawPath": "/api/db/d/query",
             "headers": {"authorization": AUTH}, "body": "{not json"}
    resp = lf.lambda_handler(event, None)
    assert resp["statusCode"] == 400


def test_bad_limit_is_400(db):
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/api/db/d/table/t",
             "headers": {"authorization": AUTH}, "queryStringParameters": {"limit": "abc"}}
    assert lf.lambda_handler(event, None)["statusCode"] == 400


def test_tx_commits_all_or_nothing(db):
    puts = db.puts
    status, data = call("POST", "/api/db/d/tx", {"statements": [
        {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["a"]},
        {"sql": "INSERT INTO t (v) VALUES (NULL)"},  # violates NOT NULL
    ]})
    assert status == 400
    assert db.puts == puts  # nothing uploaded
    _, data = call("POST", "/api/db/d/query", {"sql": "SELECT COUNT(*) AS n FROM t"})
    assert data["rows"][0][0] == 0

    status, data = call("POST", "/api/db/d/tx", {"statements": [
        {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["a"]},
        {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["b"]},
    ]})
    assert status == 200
    assert db.puts == puts + 1  # two statements, one upload
    assert data["s3"].endswith("+upload")


def test_readonly_endpoint_rejects_writes(db, monkeypatch):
    monkeypatch.setattr(lf, "READONLY", True)
    status, data = call("POST", "/api/db/d/query", {"sql": "INSERT INTO t (v) VALUES ('x')"})
    assert status == 403
    status, _ = call("POST", "/api/db/d/query", {"sql": "SELECT 1"})
    assert status == 200
    assert call("POST", "/api/databases", {"name": "e"})[0] == 403


def test_strict_table_rejects_wrong_type(db):
    status, data = call("POST", "/api/db/d/query",
                        {"sql": "INSERT INTO t (id, v) VALUES ('not-int', 'x')"})
    assert status == 400
    err = data["error"].lower()  # "datatype mismatch" or "cannot store TEXT value in INTEGER column"
    assert "mismatch" in err or "integer" in err


def test_every_response_has_ms(db):
    _, data = call("GET", "/api/databases")
    assert "ms" in data
