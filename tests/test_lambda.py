"""db lambda 的 HTTP 层:输入校验先于一切、事务接口的原子性、只读入口。"""
import base64
import json

import pytest

import lambda_function as lf

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


def test_bad_credentials(db):
    assert call("GET", "/api/databases", auth="Basic " + base64.b64encode(b"a:b").decode())[0] == 401


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
        {"sql": "INSERT INTO t (v) VALUES (NULL)"},  # 违约
    ]})
    assert status == 400
    assert db.puts == puts  # 没上传
    _, data = call("POST", "/api/db/d/query", {"sql": "SELECT COUNT(*) AS n FROM t"})
    assert data["rows"][0][0] == 0

    status, data = call("POST", "/api/db/d/tx", {"statements": [
        {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["a"]},
        {"sql": "INSERT INTO t (v) VALUES (?)", "params": ["b"]},
    ]})
    assert status == 200
    assert db.puts == puts + 1  # 两条一次上传
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
