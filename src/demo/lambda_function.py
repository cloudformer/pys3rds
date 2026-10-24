"""srds demo:示例业务 Lambda。

业务服务不直接碰 S3——把 srds db lambda 当作 RDS endpoint,
通过 pys3mysql 驱动(零依赖,接口同 pymysql)连过去。
ECS / EC2 / 本地脚本的接入方式一模一样:pip install pys3mysql + 四个连接参数。

GET  /items          列表
POST /items {text}   插入一条
"""
import base64
import json
import os

import pys3mysql

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "")  # demo 自己 API 的门,与数据库凭证无关

# 连接参数与 RDS 完全同构。未来迁真 RDS:环境变量换成 RDS 的值,
# import pys3mysql → import pymysql,下面这几行一个字不改。
DB = pys3mysql.connect(
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
    """冷启动后第一次访问前把库和表建好(两步都幂等;失败则下次再试)。"""
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
    data = json.loads(raw)  # 不合法 → ValueError → 400,在碰数据库之前拦下
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
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
    except pys3mysql.Error as e:
        return _json(502, {"error": f"database: {e}"})
    except Exception as e:
        return _json(500, {"error": f"{type(e).__name__}: {e}"})
