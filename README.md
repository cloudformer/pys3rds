# s3rds

[![PyPI](https://img.shields.io/pypi/v/s3rds)](https://pypi.org/project/s3rds/)
[![Python](https://img.shields.io/pypi/pyversions/s3rds)](https://pypi.org/project/s3rds/)
[![Tests](https://github.com/cloudformer/s3rds/actions/workflows/test.yml/badge.svg)](https://github.com/cloudformer/s3rds/actions/workflows/test.yml)
[![Compatibility](https://img.shields.io/badge/compatibility-report-blue)](tests/compat/REPORT.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

[中文](README.zh.md) · Part of *Redefine Serverless* — AWS primitives that cost ≈ $0 when idle.

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
cp terraform/example.json terraform/my.json        # set aws_profile, env, console_auth_password
terraform -chdir=terraform init
terraform -chdir=terraform apply -var-file=my.json
terraform -chdir=terraform output                   # db_url, db_ro_url
```

This creates one S3 bucket (`<env>-srds-db-2026`), two Lambdas (read/write, read-only) with Function URLs, their
log groups and one scoped IAM role. `terraform -chdir=terraform destroy -var-file=my.json` removes everything,
databases included.

### Security
- **Public URL, password inside**: with `function_url_auth = "NONE"` the Function URLs are public, so their resource
  policy says `principal = "*"` — AWS requires that for a public URL, and it only lets a request *reach* the Lambda.
  Every request must then pass HTTP Basic (`console_auth_username` / `console_auth_password`) or gets 401.
  `function_url_auth = "AWS_IAM"` makes AWS check a SigV4 signature first (the browser console cannot sign, so it is unusable then).
- **No CORS**: the console is served by the same URL and the drivers are not browsers.
- **S3**: private bucket (public access blocked), SSE-S3 encryption, a bucket policy that denies any request not over TLS.
- **IAM**: the Lambdas may read / write `db/*.sqlite`, list `db/` and write their own two log groups — nothing else.
- **Logs**: one log group per Lambda, kept 14 days.
- **Console off**: `"console": false` leaves the API only (`GET /` returns 404).

## Usage

### Console

Open `db_url` in a browser and sign in with `console_auth_username` / `console_auth_password`.

![s3rds console](docs/console.jpg)

### Connect from code

```bash
pip install s3rds
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

| Driver | You write | Not supported (raises `NotSupportedError`) |
|---|---|---|
| `s3rds.sqlite` | SQLite, no translation; SQLAlchemy and Django go through it | `create_function` (would run on the server) |
| `s3rds.pymysql` | MySQL, translated to SQLite on the client (sqlglot); pymysql's types and error codes | procedures, triggers, `FULLTEXT`, `FOR UPDATE`, `LOCK TABLES`, users |
| `s3rds.psycopg2` | PostgreSQL, translated the same way; psycopg2's `extras` / `errors` / `extensions` | functions, triggers, `LISTEN`, `COPY`, locks, sequences, arrays |

Size: best under ~20 MB (warm reads 20–50 ms, writes a few hundred ms); above ~50 MB move to RDS —
[MIGRATION.md](MIGRATION.md). Not for high-concurrency OLTP.

## Compatibility

Every feature is one test run with the same SQL on the reference — MySQL 8.0 and PostgreSQL 16 in Docker,
`sqlite3`, SQLAlchemy and Django on SQLite — and on s3rds, comparing rows, Python types, rowcount and error codes.
The generated result: **[tests/compat/REPORT.md](tests/compat/REPORT.md)**.

<details>
<summary>Verified versions</summary>

| | Version |
|---|---|
| Python (client) | 3.9 – 3.13 (regression on 3.9.6, full comparison on 3.13.7) |
| Server | AWS Lambda `python3.12`, SQLite 3.50.4 (`pip install "s3rds[server]"`) |
| sqlglot | 30.24 |
| SQLAlchemy | 2.0 (tested 2.0.55), `pip install "s3rds[sqlalchemy]"` |
| Django | 6.1 (tested 6.1.2, Python ≥ 3.12), `pip install "s3rds[django]"` |
| References | MySQL 8.0.46, PostgreSQL 16.4 in Docker |
</details>

## Development

```bash
.venv/bin/python -m pytest tests -q                                      # regression + compatibility
.venv/bin/python -m pytest tests -q --docker run --aws real-srds.json    # + MySQL / PostgreSQL in Docker vs the deployed Lambda
```

Layout: `client/` the pip package (drivers + `s3rds/server/`, the Lambda) · `terraform/` · `tests/`.
Release: test, bump the version (`client/pyproject.toml`, `__version__`, `server_package` in
`terraform/example.json`), CHANGELOG, commit, `git tag vX.Y.Z && git push --tags`.
Open work: [tests/compat/TASKS.md](tests/compat/TASKS.md).

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
