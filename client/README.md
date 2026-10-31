# s3rds

![s3rds architecture](https://raw.githubusercontent.com/cloudformer/s3rds/main/docs/architecture.svg)

### Switch to s3rds — the only change

| Your code uses | Before | With s3rds — **the only change** | What really differs |
|---|---|---|---|
| **sqlite3** | `import sqlite3`<br>`sqlite3.connect("app.db")` | `from s3rds import sqlite`<br>`sqlite.connect(host=URL, user=…, password=…, database="app")` | the file lives on S3 behind HTTPS; `create_function` is refused |
| **pymysql** | `import pymysql` | `from s3rds import pymysql` | MySQL is translated to SQLite on the client; no locks, procedures, triggers |
| **SQLAlchemy** | `create_engine("sqlite:///app.db")` | `create_engine("sqlite+s3rds://user:pw@URL/app")` | Core and ORM unchanged; `regexp_match` is refused |
| **Django** | `"ENGINE": "django.db.backends.sqlite3"` | `"ENGINE": "s3rds.sqlite"` + `HOST` / `USER` / `PASSWORD` | `__regex`, time-zone conversion in queries, `order_by("?")` are refused; run tests on sqlite3 |
| **psycopg2** | `import psycopg2` | `from s3rds import psycopg2` | PostgreSQL is translated to SQLite on the client; `.extras` / `.errors` as psycopg2's; no locks, functions, sequences, arrays; BOOLEAN comes back as 1 / 0 |

Everything else stays: models, queries, cursors, transactions. For all of them: one writer at a time, every commit
uploads the whole file (a few hundred ms), so keep a database under ~20 MB. Moving to RDS later is the same change
backwards (`import pymysql`, `import psycopg2`, `mysql+pymysql://`, `django.db.backends.mysql`). Refused features raise
`NotSupportedError` — never silently different.

### The server: deploy once, ≈ $0 when idle

You need an s3rds server in your own AWS account — a Lambda Function URL and an S3 bucket, nothing running
while nobody uses it. Deploy it once with Terraform from the
[main repository](https://github.com/cloudformer/s3rds#quick-start). Same as any driver needs its database.

**Python drivers for [s3rds](https://github.com/cloudformer/s3rds) — a truly serverless RDS:
a Lambda URL as your database endpoint, SQLite on S3, ≈ $0 when idle.**

No dependencies for the native paths; the translating drivers take an extra. Each driver is a submodule **named after
the standard client it mirrors**, so moving to a real database is a one-line import change and nothing else:

```bash
pip install s3rds                # s3rds.sqlite, sqlite+s3rds:// for SQLAlchemy, ENGINE s3rds.sqlite for Django
pip install "s3rds[mysql]"       # + s3rds.pymysql   (sqlglot: MySQL -> SQLite)
pip install "s3rds[postgres]"    # + s3rds.psycopg2  (sqlglot: PostgreSQL -> SQLite)
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

| Submodule | Kind | SQL dialect | Mirrors | Install |
|---|---|---|---|---|
| `s3rds.sqlite` | native | SQLite (no translation) | pymysql-shaped API | `s3rds` |
| `sqlite+s3rds://` | native | SQLite, written by SQLAlchemy | SQLAlchemy 2.0 Core / ORM | `s3rds` (+ SQLAlchemy) |
| `ENGINE: "s3rds.sqlite"` | native | SQLite, written by Django | Django 4.2, 5.2, 6.1 | `s3rds` (+ Django) |
| `s3rds.pymysql` | translated | MySQL, translated on the client | pymysql | `"s3rds[mysql]"` |
| `s3rds.psycopg2` | translated | PostgreSQL, translated on the client | psycopg2 (`extras`, `errors`, `extensions`) | `"s3rds[postgres]"` |

Native is the most reliable: the SQL is SQLite's to begin with. For a new project, prefer SQLAlchemy or Django.

**SQLAlchemy** (Core and ORM): only the URL changes — SQLAlchemy's own SQLite dialect writes the SQL,
`s3rds.sqlite` replaces the standard library's `sqlite3` underneath.

```python
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")
```

**Django**: Django's own SQLite backend writes the SQL; `atomic()`, savepoints and `migrate` work.

```python
DATABASES = {"default": {
    "ENGINE": "s3rds.sqlite",                       # was "django.db.backends.sqlite3"
    "NAME": "blog", "HOST": "xxxx.lambda-url.us-east-2.on.aws", "USER": "admin", "PASSWORD": "...",
}}
```

Django's SQLite backend registers Python functions on the connection; its date functions (`__year`, `__date`,
`Trunc`, `Extract`) are written in SQLite's `strftime()` instead, the rest raise `NotSupportedError`: time-zone
conversion in queries (keep `TIME_ZONE` equal to the connection's), `__regex`, `MD5` / `SHA*`, `LPad` / `Repeat` /
`Reverse`, `order_by("?")`, `StdDev` / `Variance`, date arithmetic in the query. Run `manage.py test` on
`django.db.backends.sqlite3`.

## Semantics

- **Transactions**: by default each `execute()` is its own transaction and is durable in S3 when it
  returns (`autocommit=True`). `commit()` is a no-op. `rollback()` **cannot undo statements that already
  ran** — it warns if anything was written since the last `commit()` (and stays silent otherwise, so
  pools may call it freely). For all-or-nothing writes use `transaction([...])`, which runs several
  statements as one commit and one S3 write.
- `s3rds.sqlite.connect(..., autocommit=False)` (what SQLAlchemy uses) and a `BEGIN` statement (Django's
  `atomic()`) give sqlite3's transactions: reads see
  the transaction's own uncommitted writes, `commit()` writes S3 once, `rollback()` discards. If another
  writer commits first, the transaction raises `OperationalError` instead of overwriting: run it again.
  Keep transactions short — each statement replays the writes before it on the server.
- Large results are paged by the server (4 MB per page) and reassembled inside `execute()`; `rows`
  is always complete. If the database changes while paging, the query is re-run rather than mixing versions.
- Writes **never** duplicate: the driver re-sends only when the request provably never reached the
  server (stale keep-alive connection). Timeouts raise an error with an explicit "result unknown".
- `s3rds.pymysql` raises pymysql-style exceptions with MySQL error codes (1062, 1146, 1054, 1064, …);
  unsupported MySQL features raise `NotSupportedError` instead of misbehaving silently.
- `s3rds.psycopg2` raises psycopg2's classes (`s3rds.psycopg2.errors.UniqueViolation`, …) with the SQLSTATE in `pgcode`.

## Versions

What the report was verified with; the full report: [REPORT.md](https://github.com/cloudformer/s3rds/blob/main/tests/compat/REPORT.md).

| | Version | Fixed by |
|---|---|---|
| Python (client) | 3.9, 3.10, 3.11, 3.12, 3.13, each in CI | `requires-python = ">=3.9"` |
| Server runtime | AWS Lambda `python3.12` | `terraform/main.tf` |
| SQLite (server engine) | 3.50.4, shipped in the Lambda zip (the runtime's own is 3.40.0) | extra `pip install "s3rds[server]"` = `sqlean.py==3.50.4.5` |
| sqlglot (MySQL / PostgreSQL translation) | 30.24 | extras `"s3rds[mysql]"` / `"s3rds[postgres]"` = `sqlglot>=30.24,<31` |
| SQLAlchemy | 2.0, tested on 2.0.55 | extra `pip install "s3rds[sqlalchemy]"` = `SQLAlchemy>=2.0,<2.1` |
| Django | 4.2 (Python 3.9, 3.10), 5.2 (3.11, 3.12), 6.1 (3.12, 3.13), each in CI | extra `pip install "s3rds[django]"` = `Django>=4.2,<6.2` |
| MySQL (reference) | 8.0.46 in Docker | `tests/compat/docker-compose.yml`: `mysql:8.0.46` |

Part of [IdleFree](https://cloudformer.github.io/) — AWS services and self-hosted apps rebuilt to cost ≈ $0 when idle.
