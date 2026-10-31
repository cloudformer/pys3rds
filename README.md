# s3rds

[![PyPI](https://img.shields.io/pypi/v/s3rds)](https://pypi.org/project/s3rds/)
[![Python](https://img.shields.io/pypi/pyversions/s3rds)](https://pypi.org/project/s3rds/)
[![Tests](https://github.com/cloudformer/s3rds/actions/workflows/test.yml/badge.svg)](https://github.com/cloudformer/s3rds/actions/workflows/test.yml)
[![Compatibility](https://img.shields.io/badge/compatibility-report-blue)](tests/compat/REPORT.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文](README.zh.md) · Part of **[IdleFree](https://cloudformer.github.io/)** — AWS primitives that cost ≈ $0 when idle.

## Overview

A truly serverless RDS in your own AWS account: a Lambda Function URL is the database endpoint, each database
is one SQLite file on S3, and nothing runs between requests. Your code keeps its driver — `from s3rds import pymysql`
today, `import pymysql` on a real RDS later.

**Use case:** personal apps, internal tools, blogs, MVPs — small databases (up to ~20 MB) with a few writes per second.

**Cost:** ≈ $0 when idle (S3 storage only). The cheapest RDS is ~$14 / month, Aurora Serverless v2 ~$45.

## What it does

- **SQL over HTTPS**: one Lambda Function URL per endpoint, HTTP Basic user / password like an RDS master user
- **SQLite on S3**: one database = one `.sqlite` object; reads come from a warm `/tmp` copy, writes go back with a conditional PUT
- **Drop-in drivers**: `sqlite3`, `pymysql`, `psycopg2`, SQLAlchemy and Django each need one changed line
- **Web console**: databases, tables, rows and a SQL box, served by the same Lambda
- **Read-only endpoint**: the same code with unlimited concurrency for read-heavy work

## Architecture

![s3rds architecture](docs/architecture.svg)

**Flow:**
1. The driver sends one SQL statement (or a transaction) as JSON to the Function URL
2. The Lambda checks the object's ETag; unchanged → the `/tmp` copy is used, no download
3. The statement runs in a transaction on SQLite 3.50.4
4. Only if data changed, the file goes back to S3 with `If-Match` (compare-and-swap); a conflict re-downloads and replays
5. The reply carries rows, `ms` (server time) and `s3` (`cache-hit` / `download` / `+upload`)

## Key features

- **Atomic**: every request is one transaction (DDL included) and one atomic S3 PUT
- **One writer**: reserved concurrency 1 plus the `If-Match` CAS — a concurrent write is retried, never overwritten
- **Durable**: the client gets OK only after S3 stored the file (11 nines)
- **No duplicate writes**: the driver re-sends only when the request provably never reached the server
- **Never silently different**: anything SQLite cannot do raises `NotSupportedError`

<details>
<summary>Transactions in detail</summary>

By default (`autocommit=True`) every statement is committed and durable when `execute()` returns. Several
statements that belong together: `conn.transaction([...])` (one commit, one S3 write) or `executemany(...)`.

`connect(..., autocommit=False)` (what SQLAlchemy and Django use) gives sqlite3's transactions: reads see the
transaction's own writes, `commit()` writes S3 once, `rollback()` discards, savepoints work. The server keeps no
session: each statement carries the transaction's writes so far and the server replays them. If another writer
commits in between, the next statement raises `OperationalError` ("transaction conflict") — run it again. Keep
transactions short; the cost grows with their length.

Large results are paged at 4 MB and fetched completely inside `execute()`; nothing is truncated silently.
</details>

## Deployment

### Prerequisites
- AWS account and CLI credentials
- Terraform 1.5+
- Python 3 with pip (Terraform installs the server package into the Lambda zip)

### Deploy steps
```bash
cp terraform/example.json terraform/my.json        # set aws_profile, env, console_auth_password (16+ characters)
terraform -chdir=terraform init
terraform -chdir=terraform apply -var-file=my.json
terraform -chdir=terraform output                   # db_url, db_ro_url
```

This creates one S3 bucket (`<env>-srds-db-2026`), the read/write Lambda and, with `"db_ro": true`, the read-only
Lambda, each with a Function URL, its own log group and its own IAM role. `terraform -chdir=terraform destroy
-var-file=my.json` removes everything, databases included (`"force_destroy": true`, the default); set it to `false`
and destroy stops at a non-empty bucket, so the databases survive.

### Security
- **Public URL, password inside**: with `function_url_auth = "NONE"` the Function URLs are public, so their resource
  policy says `principal = "*"` — AWS requires that for a public URL, and it only lets a request *reach* the Lambda.
  Every request must then pass HTTP Basic (`console_auth_username` / `console_auth_password`) or gets 401.
  `function_url_auth = "AWS_IAM"` makes AWS check a SigV4 signature first (the browser console cannot sign, so it is unusable then).
- **No credentials, no access**: a server without a user and password answers every API request with 503; it
  never falls back to no auth. The password must be at least 16 characters.
- **One request, one database**: `ATTACH`, `DETACH` and `VACUUM INTO` are refused (403), so SQL sent to one database
  cannot open another database's file or write files on the Lambda.
- **No CORS**: the console is served by the same URL and the drivers are not browsers.
- **S3**: private bucket (public access blocked), SSE-S3 encryption, a bucket policy that denies any request not over TLS.
- **IAM, one role per endpoint**: the read/write role may read and write `db/*.sqlite`; the read-only role may only
  read it, so the read-only endpoint cannot write to S3 even through a bug. Both may list `db/` and write their own
  log group — nothing else.
- **Logs**: one log group per Lambda, kept 14 days.
- **Console off**: `"console": false` leaves the API only (`GET /` returns 404).

## Usage

### Console

Open `db_url` in a browser and sign in with `console_auth_username` / `console_auth_password`.

![s3rds console](docs/console.jpg)

### Connect from code

```bash
pip install s3rds                # s3rds.sqlite, SQLAlchemy (sqlite+s3rds://), Django (ENGINE s3rds.sqlite): no dependencies
pip install "s3rds[mysql]"       # + s3rds.pymysql      (brings sqlglot, the MySQL -> SQLite translation)
pip install "s3rds[postgres]"    # + s3rds.psycopg2     (the same for PostgreSQL)
```

```python
from s3rds import sqlite       # SQLite dialect
from s3rds import pymysql      # MySQL dialect           later on RDS: import pymysql
from s3rds import psycopg2     # PostgreSQL dialect      later: import psycopg2

conn = pymysql.connect(host="xxxx.lambda-url.us-east-2.on.aws", user="admin", password="...", database="blog")
with conn.cursor() as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())

# SQLAlchemy (Core / ORM): only the URL changes
engine = create_engine("sqlite+s3rds://admin:...@xxxx.lambda-url.us-east-2.on.aws/blog")

# Django: only ENGINE and the connection settings change
DATABASES = {"default": {"ENGINE": "s3rds.sqlite", "NAME": "blog",
                         "HOST": "xxxx.lambda-url.us-east-2.on.aws", "USER": "admin", "PASSWORD": "..."}}
```

Any other language: plain HTTPS + JSON.

```bash
curl -u admin:password -d '{"sql":"SELECT * FROM posts WHERE id = ?","params":[1]}' \
     https://xxxx.lambda-url.us-east-2.on.aws/api/db/blog/query
```

## Compared with RDS

| | **s3rds** | RDS `db.t4g.micro` single-AZ | RDS `db.t4g.micro` Multi-AZ | Aurora Serverless v2 |
|---|---|---|---|---|
| Idle cost / month (us-east-2) | **≈ $0** (S3 storage only) | ~$14 | ~$28 | ~$45 (0.5 ACU floor) |
| Availability zones | regional, multi-AZ by construction | one | two | multi |
| Instance failure | no instance | minutes of downtime | failover ~60 s | failover |
| Maintenance window | none | yes, with restarts | yes | yes |
| SLA | Lambda 99.95% · S3 99.9% | none for single-AZ | 99.95% | 99.99% |
| Durability | S3, 11 nines | EBS | EBS, replicated | 6 copies |
| SQL | SQLite, MySQL and PostgreSQL dialects | MySQL | MySQL | MySQL / PostgreSQL |

## Limits

Size: best under ~20 MB (warm reads 20–50 ms, writes a few hundred ms); above ~50 MB move to RDS —
[MIGRATION.md](MIGRATION.md). Not for high-concurrency OLTP.

## Compatibility

| You use | Kind | Tested versions | Not supported (raises `NotSupportedError`) |
|---|---|---|---|
| `sqlite3` → `s3rds.sqlite` | **native**: SQLite SQL, no translation | Python 3.9 – 3.13 | `create_function` (would run on the server) |
| SQLAlchemy → `sqlite+s3rds://` | **native**: SQLAlchemy's SQLite dialect writes the SQL | SQLAlchemy 2.0 | `regexp_match` |
| Django → `ENGINE "s3rds.sqlite"` | **native**: Django's SQLite backend writes the SQL | Django 4.2, 5.2, 6.1 | `__regex`, time-zone conversion in queries, `order_by("?")`, `MD5` / `SHA*`, `StdDev` / `Variance` |
| `pymysql` → `s3rds.pymysql` | **translated**: MySQL → SQLite on the client (sqlglot) | vs MySQL 8.0 | procedures, triggers, `FULLTEXT`, `SELECT … FOR UPDATE`, `LOCK TABLES`, users |
| `psycopg2` → `s3rds.psycopg2` | **translated**: PostgreSQL → SQLite on the client (sqlglot) | vs PostgreSQL 16 | functions, triggers, `LISTEN`, `COPY`, locks, sequences, arrays |

Native is the most reliable: the SQL is SQLite's to begin with. Translation has edges, each listed in the report.
**For a new project, prefer SQLAlchemy or Django.**

Every feature is one test run with the same SQL on the reference — MySQL 8.0 and PostgreSQL 16 in Docker,
`sqlite3`, SQLAlchemy and Django on SQLite — and on s3rds, comparing rows, Python types, rowcount and error codes.
CI runs every Python × Django combination above. The generated result, every feature and every gap:
**[tests/compat/REPORT.md](tests/compat/REPORT.md)**.

<details>
<summary>Verified versions</summary>

| | Version |
|---|---|
| Python (client) | 3.9, 3.10, 3.11, 3.12, 3.13 (CI) |
| Server | AWS Lambda `python3.12`, SQLite 3.50.4 (`pip install "s3rds[server]"`) |
| sqlglot | 30.24, only with `s3rds[mysql]` / `s3rds[postgres]` |
| SQLAlchemy | 2.0 (tested 2.0.55), `pip install "s3rds[sqlalchemy]"` |
| Django | 4.2 (Python 3.9, 3.10), 5.2 (3.11, 3.12), 6.1 (3.12, 3.13) in CI, `pip install "s3rds[django]"` |
| References | MySQL 8.0.46, PostgreSQL 16.4 in Docker |
</details>

## Development

```bash
.venv/bin/python -m pytest tests -q                                      # regression + compatibility
.venv/bin/python -m pytest tests -q --docker run --aws real-srds.json    # + MySQL / PostgreSQL in Docker vs the deployed Lambda
```

Layout: `client/` the pip package (drivers + `s3rds/server/`, the Lambda) · `terraform/` · `tests/`.
Terraform deploys the server from this working tree (`./client[server]`), so `apply` runs what you test.
Release: test, bump the version (`client/pyproject.toml`, `__version__`), CHANGELOG, commit,
`git tag vX.Y.Z && git push --tags`.
Open work: [tests/compat/TASKS.md](tests/compat/TASKS.md).

## Part of IdleFree

Part of **[IdleFree](https://cloudformer.github.io/)** — AWS services and self-hosted apps rebuilt to cost ≈ $0 when
idle. MIT.

## License

MIT
