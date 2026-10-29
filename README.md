# s3rds

![s3rds architecture](docs/architecture.svg)

### Switch to s3rds — the only change

| Your code uses | Before | With s3rds — **the only change** | What really differs |
|---|---|---|---|
| **sqlite3** | `import sqlite3`<br>`sqlite3.connect("app.db")` | `from s3rds import sqlite`<br>`sqlite.connect(host=URL, user=…, password=…, database="app")` | the file lives on S3 behind HTTPS; `create_function` is refused |
| **pymysql** | `import pymysql` | `from s3rds import pymysql` | MySQL is translated to SQLite on the client; no locks, procedures, triggers |
| **SQLAlchemy** | `create_engine("sqlite:///app.db")` | `create_engine("sqlite+s3rds://user:pw@URL/app")` | Core and ORM unchanged; `regexp_match` is refused |
| **Django** | `"ENGINE": "django.db.backends.sqlite3"` | `"ENGINE": "s3rds.sqlite"` + `HOST` / `USER` / `PASSWORD` | `__regex`, time-zone conversion in queries, `order_by("?")` are refused; run tests on sqlite3 |
| **psycopg2** | `import psycopg2` | `from s3rds import psycopg2` — *planned, not available yet* | — |

Everything else stays: models, queries, cursors, transactions. For all of them: one writer at a time, every commit
uploads the whole file (a few hundred ms), so keep a database under ~20 MB. Moving to RDS later is the same change
backwards (`import pymysql`, `mysql+pymysql://`, `django.db.backends.mysql`). Refused features raise
`NotSupportedError` — never silently different.

### The server: deploy once, ≈ $0 when idle

You need an s3rds server in your own AWS account — a Lambda Function URL and an S3 bucket, nothing running
while nobody uses it. Deploy it once with Terraform or CloudFormation ([Quick start](#quick-start)).
Same as any driver needs its database.

**A truly serverless RDS: a Lambda URL is your database endpoint, SQLite on S3 is your storage, ≈ $0 when idle.**

[中文](README.zh.md) · Part of *Redefine Serverless* — AWS primitives that cost ≈ $0 when idle.

| | **s3rds** | RDS `db.t4g.micro` single-AZ | RDS `db.t4g.micro` Multi-AZ | Aurora Serverless v2 |
|---|---|---|---|---|
| Idle cost / month (us-east-2) | **≈ $0** (S3 storage only) | ~$14 | ~$28 | ~$45 (0.5 ACU floor) |
| Availability zones | regional, multi-AZ by construction | one | two | multi |
| Instance failure | no instance | minutes of downtime | failover ~60 s | failover |
| Maintenance window | none | yes, with restarts | yes | yes |
| SLA | Lambda 99.95% · S3 99.9% | none for single-AZ | 99.95% | 99.99% |
| Durability | S3, 11 nines | EBS | EBS, replicated | 6 copies |
| SQL compatibility | SQLite dialect, MySQL dialect via `s3rds.pymysql`, SQLAlchemy Core / ORM via `sqlite+s3rds://`, Django ORM via `ENGINE: "s3rds.sqlite"` — see the generated [compatibility report](tests/compat/REPORT.md) | MySQL | MySQL | MySQL / PostgreSQL |

> Design principle: this is a relational database, so correctness comes first — atomicity and
> consistency are not negotiable. A write may fail, but it never duplicates and never corrupts.
> The upside is cost (≈ $0 idle); the trade-off is performance. Order of priorities: correctness > cost > performance.

## Why

"Serverless" on AWS usually means *the box scales*. It rarely means *there is no box*.
The cheapest provisioned RDS is ~$14/month and has no SLA; Aurora Serverless v2 still bills ~$45/month at its floor.

s3rds has no instance. Nothing runs between requests. Lambda stays inside the permanent
free tier (1M requests + 400k GB-s per month); beyond that it is $0.20 per million requests.

## How it works

- **One database = one `.sqlite` file on S3.** The db lambda downloads it to `/tmp`, runs your SQL, and
  uploads the whole file back only if the data actually changed (SQLite's authorizer + a file fingerprint decide).
- **Reads are cheap**: an ETag check against a warm `/tmp` copy, ~15 ms server-side, no download.
- **Writes are safe**: every statement runs in an explicit transaction (DDL included); the upload is a
  conditional `If-Match` PUT (compare-and-swap). A conflict re-downloads and replays; exhausted retries fail
  cleanly and invalidate the local copy. No phantom data, ever.
- **Single writer** by construction (`reserved_concurrency = 1`), plus a separate **read-only endpoint**
  with unlimited concurrency (same code, `READONLY=1`).

Mapped to RDS concepts:

| RDS | s3rds |
|---|---|
| DB instance endpoint | the db lambda's Function URL |
| a database on the instance | one `.sqlite` object in the bucket |
| `database=` in the connection string | `/api/db/{name}` in the URL |
| user / password | HTTP Basic (`db_user` / `db_password`) |
| data directory | `s3://<bucket>/db/` |
| `CREATE DATABASE` | `POST /api/databases` |

## Quick start

### 1. Deploy the server (pick one)

**Terraform**

```bash
cp terraform/example.json terraform/my.json      # set aws_profile, env, db_password
terraform -chdir=terraform init
terraform -chdir=terraform apply -var-file=my.json
terraform -chdir=terraform output                 # db_url, db_ro_url
terraform -chdir=terraform destroy -var-file=my.json   # tear everything down (the bucket must be empty)
```

**CloudFormation** (AWS CLI only)

```bash
cp cloudformation/example-params.json cloudformation/params.json   # set passwords
python3 scripts/build_server.py                                  # src/db + the pinned SQLite engine
aws cloudformation package --template-file cloudformation/template.yaml \
    --s3-bucket <any-bucket-you-own> --output-template-file /tmp/s3rds.yaml
aws cloudformation deploy --template-file /tmp/s3rds.yaml --stack-name s3rds \
    --capabilities CAPABILITY_IAM --parameter-overrides file://cloudformation/params.json
```

Both create the same thing: an S3 bucket (`<env>-srds-db-2026`), two Lambdas (read/write, read-only)
with public Function URLs, and one scoped IAM role. The bucket name is global on S3 — if it is taken, change the suffix.

### 2. Open the console

Open `db_url` in a browser, sign in with `db_user` / `db_password`. Create a database, create a table
(STRICT by default), add columns, edit rows, run SQL. Every operation shows server time and whether it hit the
`/tmp` cache or went to S3.

### 3. Connect from code

```bash
pip install s3rds    # s3rds.sqlite, s3rds.pymysql (MySQL translated on the client by sqlglot), sqlite+s3rds:// for SQLAlchemy, ENGINE s3rds.sqlite for Django
```

Pick the dialect you want to write; the API is pymysql-shaped either way, and `import pymysql`
on a real RDS is the final one-line switch:

```python
from s3rds import sqlite                            # SQLite dialect
# from s3rds import pymysql                         # MySQL dialect (see "MySQL dialect" below)

conn = sqlite.connect(host="xxxx.lambda-url.us-east-2.on.aws",
                         user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(sqlite.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())

conn.transaction([                                   # all-or-nothing, one S3 write
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

**SQLAlchemy** (Core and ORM): only the URL changes. SQLAlchemy's own SQLite dialect writes the SQL, nothing
is translated; `s3rds.sqlite` replaces the standard library's `sqlite3` underneath, with sqlite3's transactions
(see [Transactions](#guarantees)):

```python
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")
# later, on RDS:        "mysql+pymysql://admin:...@my-rds.amazonaws.com/blog"
```

An application that already supports SQLite (`DATABASE_URL=sqlite:///...`) needs no code change.

**Django**: only `ENGINE` and the connection settings change. Django's own SQLite backend writes the SQL;
`s3rds.sqlite` replaces `sqlite3` underneath, `atomic()` and savepoints are the driver's open transactions, and
`migrate` works (table rebuilds turn foreign keys off for the connection, which the driver sends with every request):

```python
DATABASES = {"default": {
    "ENGINE": "s3rds.sqlite",                       # was "django.db.backends.sqlite3"
    "NAME": "blog", "HOST": "xxxx.lambda-url.us-east-2.on.aws", "USER": "admin", "PASSWORD": "...",
}}
# later, on RDS:  "ENGINE": "django.db.backends.mysql", same NAME / HOST / USER / PASSWORD
```

Create the database once (console, or `s3rds.sqlite.connect(...).create_database()`), then `manage.py migrate`.
Run `manage.py test` on `django.db.backends.sqlite3` (the same SQL on a local file): test databases on s3rds are refused.

Not available: Python functions registered on the connection (`create_function`, SQLAlchemy's `regexp_match`) —
they would have to run on the server. Django's SQLite backend registers ~35 of them: its date functions
(`__year`, `__date`, `Trunc`, `Extract`, `dates()`) are written in SQLite's own `strftime()` instead and compared
with the originals; the rest raise `NotSupportedError` — time-zone conversion in queries (keep `TIME_ZONE` equal to
the connection's, UTC by default), `__regex`, `MD5` / `SHA*`, `LPad` / `RPad` / `Repeat` / `Reverse`, `order_by("?")`,
`StdDev` / `Variance`, date arithmetic in the query (`F("dt") + timedelta`, `F("end") - F("start")`).

One dependency, sqlglot, used only by `s3rds.pymysql`. The wire protocol is plain HTTPS + JSON, so any language works:

```bash
curl -s -u "admin:password" -H "content-type: application/json" \
     -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     "https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query"
```

## Compatibility

The compatibility matrix is generated, not hand-written: **[tests/compat/REPORT.md](tests/compat/REPORT.md)**.
Every row is one feature (DDL, DML, queries, functions, constraints, metadata, types) run with the same SQL on the
reference database — MySQL 8.0 in Docker for `s3rds.pymysql`, the standard-library `sqlite3` for `s3rds.sqlite`,
SQLAlchemy on `sqlite3` for `sqlite+s3rds`, Django's SQLite backend for `ENGINE: "s3rds.sqlite"` — and on s3rds,
comparing results, Python types, rowcount, lastrowid and error codes.

| Status | Meaning |
|---|---|
| ✅ verified | identical to the reference; safe to rely on |
| ❌ gap | known difference; the note names the task that closes it |
| ⛔ unsupported by design | refused with `NotSupportedError` (row locks, triggers, procedures) — never silently different |
| 🔴 regression | was verified, now differs — blocks a release |

Regenerate the report (needs Docker for the MySQL reference):

```bash
python3 -m pytest tests -q                 # leaves Docker alone: live if MySQL is running, else the compat tests are skipped
python3 -m pytest tests -q --docker up     # starts MySQL in Docker, waits until healthy, tests; keeps it running
python3 -m pytest tests -q --docker run    # the same, then stops it if all passed (on a failure it keeps running)
python3 -m pytest tests -q --docker down   # the same, then stops it whatever the result
python3 -m pytest tests -q --docker run --aws real-srds.json   # MySQL in Docker vs the deployed Lambda (URL from terraform output)
```

### Versions

What the report was verified with; `client/pyproject.toml` (`[tool.s3rds.verified]`) holds the same.

| | Version | Fixed by |
|---|---|---|
| Python (client) | 3.9 – 3.13: regression suite on 3.9.6, full comparison on 3.13.7 | `requires-python = ">=3.9"` |
| Server runtime | AWS Lambda `python3.12` | `terraform/main.tf` |
| SQLite (server engine) | 3.50.4, shipped in the Lambda zip (the runtime's own is 3.40.0) | `src/db/requirements.txt`: `sqlean.py==3.50.4.5` |
| sqlglot (MySQL translation) | 30.24 | dependency `sqlglot>=30.24,<31` |
| SQLAlchemy | 2.0, tested on 2.0.55 | extra `pip install "s3rds[sqlalchemy]"` = `SQLAlchemy>=2.0,<2.1` |
| Django | 6.1, tested on 6.1.2 (Python ≥ 3.12) | extra `pip install "s3rds[django]"` = `Django>=6.1,<6.2` |
| MySQL (reference) | 8.0.46 in Docker | `tests/compat/docker-compose.yml`: `mysql:8.0.46` |

## Guarantees

| | How |
|---|---|
| **Atomicity** | explicit `BEGIN/COMMIT` per request (DDL included) + atomic S3 PUT; `/tx` runs several statements as one |
| **Consistency** | SQLite constraints; `STRICT` tables by default; `PRAGMA foreign_keys=ON` on every request unless the connection turned it off (`PRAGMA foreign_keys = OFF`, as Django's migrations do) |
| **Isolation** | one writer (reserved concurrency 1 + `If-Match` CAS); readers see a whole-file snapshot; an open transaction (`autocommit=False`) is serializable: if another writer commits first it fails, it never overwrites |
| **Durability** | the client gets OK only after the S3 PUT succeeds (11 nines) |
| **No duplicate writes** | the driver re-sends only when the request provably never reached the server; timeouts raise "result unknown" |
| **Availability** | Lambda and S3 are regional, multi-AZ services: no instance to fail, no AZ to lose, no maintenance window. Higher than a single-AZ RDS instance (which carries no SLA); Multi-AZ RDS costs 2× to match |

**Transactions, concretely.** By default (`autocommit=True`) every statement is committed and durable in S3
the moment `execute()` returns. `commit()` is a no-op. `rollback()` cannot undo statements that already ran —
the driver warns if anything was written since the last `commit()`, and stays silent otherwise. When several
statements must succeed or fail together, use `conn.transaction([...])` (one commit, one S3 write) or
`cursor.executemany(...)`.

`s3rds.sqlite.connect(..., autocommit=False)` (what SQLAlchemy uses) gives sqlite3's transactions:
`INSERT` / `UPDATE` / `DELETE` open one (and a `BEGIN` statement in either mode — Django's `atomic()`), reads inside it see its own uncommitted writes, `commit()` writes S3
once, `rollback()` discards, `SAVEPOINT` / `ROLLBACK TO` work. The server keeps no session: every statement in
a transaction carries the transaction's writes so far, the server replays them on the version the transaction
began on, runs the new statement and rolls back; `commit()` replays them once more for real. If another writer
commits in between, the next statement or `commit()` raises `OperationalError` ("transaction conflict") — roll
back and run the transaction again. The cost grows with the length of the transaction (each statement re-runs
the writes before it): keep transactions short. A write that calls `CURRENT_TIMESTAMP` or `random()` may store
a slightly different value at commit than it showed inside the transaction.

**How to write code for it** (stateless, like every Lambda): one statement = one durable write; read first,
decide, then write everything that belongs together in one `transaction([...])` or one short open transaction;
put the decision in the SQL where you can (`WHERE balance >= %s`, `ON DUPLICATE KEY UPDATE`,
`INSERT … SELECT`) instead of read-modify-write across calls; make retries safe with idempotent statements.

**Large results.** The server pages results at 4 MB and the driver fetches every page inside `execute()`,
so you always get the complete result; nothing is ever truncated silently. If the database changes while
paging, the server refuses to mix versions and the driver re-runs the query.

All of this is pinned by tests against an in-memory S3 that implements ETag / `If-Match` / `If-None-Match` semantics:

```bash
pip install pytest boto3
pytest tests/ -v
```

## MySQL dialect (`s3rds.pymysql`)

Write MySQL, run on the same endpoint. Statements are translated on the client (sqlglot, cached per statement):

- `%s` / `%(name)s` placeholders, backticks, `LIMIT offset, count`
- `AUTO_INCREMENT` → `INTEGER PRIMARY KEY AUTOINCREMENT`; `ENUM(...)` → `TEXT CHECK (...)`; `UNSIGNED` → signed;
  text columns get `COLLATE NOCASE` (MySQL's case-insensitive default; `_bin` collations stay case-sensitive)
- inline `KEY` / `INDEX` / `UNIQUE KEY name (a, b)` become `CREATE INDEX` statements in the same transaction
  (stored as `table__name`, shown and dropped by their MySQL name)
- `INSERT IGNORE`, `ON DUPLICATE KEY UPDATE` (`VALUES(col)` → `excluded.col`), `REPLACE INTO`, `INSERT … SELECT`,
  the `DEFAULT` keyword, `UPDATE … JOIN`, `DELETE t FROM t JOIN …`, `TRUNCATE`, `RENAME TABLE`
- `ALTER TABLE CHANGE a b` renames; a type-only `MODIFY` / `CHANGE` is accepted and does nothing (SQLite is dynamically typed)
- functions: `DATE_ADD/SUB`, `YEAR … SECOND`, `DAYOFWEEK`, `DATEDIFF`, `TIMESTAMPDIFF`, `LPAD/RPAD`, `REPEAT`,
  `SUBSTRING_INDEX` (count ±1), `->` / `->>` / `JSON_UNQUOTE`, `UUID()`, `RAND()`, `LAST_INSERT_ID()`, `DATABASE()`
- `SHOW TABLES / DATABASES / COLUMNS / CREATE TABLE / INDEX`, `DESCRIBE`, `information_schema.tables / columns`;
  `USE db`, `SET NAMES / SESSION / sql_mode / time_zone` are accepted as no-ops
- errors are pymysql-style classes with MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, …
- `ENGINE=`, `CHARSET=`, `COMMENT`, `ON UPDATE CURRENT_TIMESTAMP` are dropped

Not supported — raises `NotSupportedError`, never silently wrong: stored procedures/functions, triggers,
`FULLTEXT`, `SELECT … FOR UPDATE`, `LOCK TABLES`, `GRANT`/users.

**Who this is for:** code that writes MySQL — pymysql-style calls, your own data-access layer. An ORM goes
through the ORM's SQLite support instead (`sqlite+s3rds://` for SQLAlchemy, `ENGINE: "s3rds.sqlite"` for Django,
see [Connect from code](#3-connect-from-code)): the MySQL dialects would need the server session they probe
(`SHOW VARIABLES`). If you need an ORM on MySQL, that is the moment to move to RDS — see [MIGRATION.md](MIGRATION.md).

Result types match pymysql: `DATETIME` → `datetime`, `DATE` → `date`, `TIME` → `timedelta`, `DECIMAL` /
`SUM` / `AVG` / integer division → `Decimal`, `BLOB` → `bytes` (both directions), `NOW()` → `datetime`.
Strict mode is emulated: `'abc'` into an `INT` column raises 1366. `DESCRIBE` and `SHOW CREATE TABLE`
show the MySQL types you declared (kept in a small `_s3rds_columns` table inside the database).

**Compatibility report — [tests/compat/REPORT.md](tests/compat/REPORT.md).** Every feature above is one
test that runs the same SQL on a real MySQL 8.0 (Docker) and on s3rds and compares results, Python types,
rowcount, lastrowid and error codes. Verified rows may be relied on; gap rows name the task that closes them.
The report is regenerated by every run (commands under [Compatibility](#compatibility)).

## Where it fits

| Database size | Verdict |
|---|---|
| **< 10 MB** | sweet spot — warm reads 20–50 ms, cold read / write ~300 ms |
| **10–20 MB** | design limit — writes a few hundred ms, still smooth |
| **20–50 MB** | works, but each write re-uploads the file (~1 s); frequent writes get annoying |
| **> 50 MB** | move to a real RDS — see [MIGRATION.md](MIGRATION.md) |

Reads do not degrade with size (ETag cache); only cold reads and writes move the whole file.
Write throughput is a few per second. Good for personal apps, internal tools, blogs, MVPs; not for high-concurrency OLTP.

`scripts/bench.py` measures end-to-end latency and throughput against your deployment and cleans up after itself.

## Repository layout

```
src/db/           the database: lambda_function.py (HTTP API), s3_sqlite.py (engine), index.html (console)
client/           the pip package s3rds: s3rds/sqlite/ (driver; base.py = the Django backend), s3rds/pymysql.py, s3rds/psycopg2.py, pyproject.toml
terraform/        deployment, option A
cloudformation/   deployment, option B
tests/            correctness guarantees
scripts/          bench.py
MIGRATION.md      how to move to RDS MySQL when you outgrow this
```

## Releasing

CI (`.github/workflows/test.yml`) runs the whole suite, MySQL included, on every push. A release is a tag:

```bash
# bump version in client/pyproject.toml and client/s3rds/sqlite/__init__.py, add the CHANGELOG entry, commit, then
git tag v0.2.1 && git push --tags      # tests → build client/ → PyPI (trusted publishing, no token)
```

## Roadmap

Open work is tracked in [tests/compat/TASKS.md](tests/compat/TASKS.md). Done: open transactions and SQLAlchemy
(0.2.0), Django (0.2.1). Next, 0.3.0: `s3rds.psycopg2` (PostgreSQL dialect; the placeholder module ships today and
`connect()` raises until implemented) and a one-click CloudFormation
*Launch Stack* button.

## The Redefine Serverless series

AWS "serverless" services that still bill while idle, rebuilt on primitives that cost ≈ $0 when idle.
Same rule everywhere: project = pip = import name; each submodule is named after the standard client
it mirrors, so going back to the real service is one import line.

| Replaces | Idle floor / month | Built on | Project | Usage | Status |
|---|---|---|---|---|---|
| RDS / Aurora Serverless | ~$14–45 | S3 + Lambda | **s3rds** | `from s3rds import pymysql` · `sqlite` · `psycopg2` | this repo |
| Redis / ElastiCache Serverless | ~$90 | DynamoDB | **pyredis** | `from pyredis import redis` | next |
| Airflow / MWAA | ~$360 | Lambda + EventBridge + Step Functions | **lambdaflow** | `from lambdaflow import airflow` | planned |
| Redshift Serverless | ~$3/h once it wakes | S3 + DuckDB | **s3shift** | `from s3shift import redshift_connector` | planned |
| OpenSearch Serverless | ~$350 | S3 + SQLite FTS5 (on s3rds) | **s3search** | `from s3search import opensearchpy` | planned |
| Kafka / MSK Serverless | ~$550 | SQS | **sqskafka** | `from sqskafka import kafka` | planned |
| DocumentDB (MongoDB) | ~$200 | SQLite JSON1 (on s3rds) | **s3mongo** | `from s3mongo import pymongo` | planned |
| Amazon MQ (RabbitMQ) | ~$20–50 | SQS | **sqsmq** | `from sqsmq import pika` | planned |

Also covered by the above: MemoryDB (→ pyredis), Kinesis Data Streams (→ sqskafka), Timestream (→ s3shift).

Shared principles: ≈ $0 when idle, no container and no instance, drop-in standard interfaces,
one test suite run against the real service and the rebuild, MIT.

## License

MIT
