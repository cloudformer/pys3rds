"""s3rds demo: a sample business Lambda.

The service never touches S3: it treats the s3rds endpoint as an RDS endpoint,
through the s3rds driver (no dependencies, pymysql-shaped API).
ECS / EC2 / a local script connect the same way: pip install s3rds + four connection parameters.

GET  /items          list
POST /items {text}   insert one
"""
import base64
import json
import os

from s3rds import sqlite

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")  # the demo API's own gate, unrelated to the database credentials

# the connection parameters have the same shape as RDS. Moving to a real RDS: point the env vars at it,
# `from s3rds import pymysql` -> `import pymysql`, and the lines below do not change.
DB = sqlite.connect(
    host=os.environ["DB_ENDPOINT"],
    user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"],
    database=os.environ["DB_NAME"],
)

SCHEMA = """CREATE TABLE IF NOT EXISTS items (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  text       TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_initialized = False


def _ensure_init():
    """Create the database and table before the first request after a cold start (both idempotent; retried next time on failure)."""
    global _initialized
    if not _initialized:
        DB.create_database()
        DB.execute(SCHEMA)
        _initialized = True


def _json(status, data):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(data, ensure_ascii=False, default=str),
    }


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw)
    data = json.loads(raw)  # invalid -> ValueError -> 400, before the database is touched
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def lambda_handler(event, context):
    http = (event.get("requestContext") or {}).get("http") or {}
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")

    if AUTH_TOKEN and (event.get("headers") or {}).get("x-auth-token") != AUTH_TOKEN:
        return _json(401, {"error": "bad token"})

    try:
        if method == "GET" and path == "/items":
            _ensure_init()
            return _json(200, {"items": DB.query("SELECT * FROM items ORDER BY id DESC")})

        if method == "POST" and path == "/items":
            text = (_body(event).get("text") or "").strip()
            if not text:
                return _json(400, {"error": "text is required"})
            _ensure_init()
            r = DB.execute("INSERT INTO items (text) VALUES (?)", [text])
            return _json(201, {"id": r.get("lastrowid")})

        return _json(404, {"error": f"no route: {method} {path}"})
    except ValueError as e:
        return _json(400, {"error": f"bad request: {e}"})
    except sqlite.Error as e:
        return _json(502, {"error": f"database: {e}"})
    except Exception as e:
        return _json(500, {"error": f"{type(e).__name__}: {e}"})
