# s3rds

**Python drivers for [s3rds](https://github.com/cloudformer/s3rds) — a truly serverless RDS:
a Lambda URL as your database endpoint, SQLite on S3, ≈ $0 when idle.**

![s3rds architecture](https://raw.githubusercontent.com/cloudformer/s3rds/main/docs/architecture.svg)

One install, one dependency (sqlglot, used only by `s3rds.pymysql`). Each driver is a submodule **named after the standard
client it mirrors**, so moving to a real database is a one-line import change and nothing else:

```bash
pip install s3rds    # s3rds.sqlite, s3rds.pymysql, sqlite+s3rds:// for SQLAlchemy
```

```python
from s3rds import pymysql        # today   -- later: `import pymysql` on a real RDS, rest unchanged

conn = pymysql.connect(
    host="xxxx.lambda-url.us-east-2.on.aws",
    user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("CREATE TABLE posts (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(200))")
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(pymysql.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())                         # [{'id': 1, 'title': 'hello'}]

conn.transaction([                                 # all-or-nothing, single S3 write
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

| Submodule | SQL dialect | Mirrors | Status |
|---|---|---|---|
| `s3rds.sqlite` | SQLite (no translation) | pymysql-shaped API | available |
| `s3rds.pymysql` | MySQL, translated on the client | pymysql | available |
| `s3rds.psycopg2` | PostgreSQL | psycopg2 | placeholder; `connect()` raises until implemented |
| `sqlite+s3rds://` | SQLite, written by SQLAlchemy | SQLAlchemy Core / ORM | available — the URL is the only change |

**SQLAlchemy** (Core and ORM): only the URL changes — SQLAlchemy's own SQLite dialect writes the SQL,
`s3rds.sqlite` replaces the standard library's `sqlite3` underneath.

```python
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")
```

## Semantics

- **Transactions**: by default each `execute()` is its own transaction and is durable in S3 when it
  returns (`autocommit=True`). `commit()` is a no-op. `rollback()` **cannot undo statements that already
  ran** — it warns if anything was written since the last `commit()` (and stays silent otherwise, so
  pools may call it freely). For all-or-nothing writes use `transaction([...])`, which runs several
  statements as one commit and one S3 write.
- `s3rds.sqlite.connect(..., autocommit=False)` (what SQLAlchemy uses) has sqlite3's transactions: reads see
  the transaction's own uncommitted writes, `commit()` writes S3 once, `rollback()` discards. If another
  writer commits first, the transaction raises `OperationalError` instead of overwriting: run it again.
  Keep transactions short — each statement replays the writes before it on the server.
- Large results are paged by the server (4 MB per page) and reassembled inside `execute()`; `rows`
  is always complete. If the database changes while paging, the query is re-run rather than mixing versions.
- Writes **never** duplicate: the driver re-sends only when the request provably never reached the
  server (stale keep-alive connection). Timeouts raise an error with an explicit "result unknown".
- `s3rds.pymysql` raises pymysql-style exceptions with MySQL error codes (1062, 1146, 1054, 1064, …);
  unsupported MySQL features raise `NotSupportedError` instead of misbehaving silently.

## Versions

What the report was verified with; the full report: [REPORT.md](https://github.com/cloudformer/s3rds/blob/main/tests/compat/REPORT.md).

| | Version | Fixed by |
|---|---|---|
| Python (client) | 3.9 – 3.13: regression suite on 3.9.6, full comparison on 3.13.7 | `requires-python = ">=3.9"` |
| Server runtime | AWS Lambda `python3.12` | `terraform/main.tf` |
| SQLite (server engine) | 3.50.4, shipped in the Lambda zip (the runtime's own is 3.40.0) | `src/db/requirements.txt`: `sqlean.py==3.50.4.5` |
| sqlglot (MySQL translation) | 30.24 | dependency `sqlglot>=30.24,<31` |
| SQLAlchemy | 2.0, tested on 2.0.55 | extra `pip install "s3rds[sqlalchemy]"` = `SQLAlchemy>=2.0,<2.1` |
| MySQL (reference) | 8.0.46 in Docker | `tests/compat/docker-compose.yml`: `mysql:8.0.46` |

## Server

You need an s3rds server in your own AWS account — deploy it once with Terraform or CloudFormation
from the [main repository](https://github.com/cloudformer/s3rds). Same as any driver needs its database.
