# Changelog

## 0.3.0 — 2026-10-29

PostgreSQL dialect. No server redeploy needed.

- **`s3rds.psycopg2`**: `from s3rds import psycopg2`, later `import psycopg2`. psycopg2's API: `connect(dsn)` or
  `connect(host=, dbname=, user=, password=)`, `autocommit=False` by default, `with conn:` commits / rolls back,
  `%s` / `%(name)s`. Submodules named as psycopg2's: `extras` (`RealDictCursor`, `DictCursor`, `Json`,
  `execute_values`, `execute_batch`), `errors` (`UniqueViolation`, … with `pgcode`), `extensions` (`parse_dsn`).
  PostgreSQL is translated to SQLite by sqlglot; `SERIAL` / `IDENTITY` primary keys, `RETURNING`, `ON CONFLICT`,
  `TRUNCATE … RESTART IDENTITY`, `START TRANSACTION`; NUMERIC / DATE / TIMESTAMP come back as Decimal / date / datetime.
  Refused with `NotSupportedError`: functions / triggers, LISTEN / NOTIFY, COPY, locks, roles, sequences, arrays,
  temporary tables. Compared with PostgreSQL 16.4 in Docker in `tests/compat/test_postgres.py`; gaps: tasks 28, 29.
- Tests: `compose-service` may name several containers (`"mysql postgres"`), in `compat_docker.py` and the shared
  workflow.

## 0.2.1

Django. Server redeploy required (connection settings sent with every request); with an old server, Django's
schema editor stops with "foreign key constraint checks are enabled" instead of rebuilding a table unsafely.

- **Django** (ORM, `atomic()`, savepoints, `migrate`): `DATABASES = {"default": {"ENGINE": "s3rds.sqlite", "NAME": …,
  "HOST": "<lambda-url>", "USER": …, "PASSWORD": …}}`. Django's own SQLite backend with `s3rds.sqlite` as its driver
  (`s3rds/sqlite/base.py`; `s3rds.sqlite` is now a package). Its date functions (`__year`, `__date`, `Trunc`,
  `Extract`, `dates()`), Python functions on a sqlite3 connection, are written in SQLite's `strftime()`; the other
  Python functions raise `NotSupportedError`: time-zone conversion in queries, `__regex`, `MD5` / `SHA*`, `LPad` /
  `RPad` / `Repeat` / `Reverse` / `Cot`, `order_by("?")`, `StdDev` / `Variance`, date arithmetic in the query.
  Compared with Django's SQLite backend in `tests/compat/test_django.py`. Extra `pip install "s3rds[django]"`
  (Django 6.1, tested on 6.1.2).
- **Connection PRAGMAs**: `PRAGMA foreign_keys = ON / OFF` and `PRAGMA legacy_alter_table` are kept by the driver
  per connection, as sqlite3 does, and applied by the server to every request (`pragmas` in the request body).
- `BEGIN` opens a transaction in autocommit mode too (sqlite3 with `isolation_level=None`; Django's `atomic()`);
  `rollback()` of an open transaction no longer warns about earlier autocommit writes.
- `s3rds.pymysql`: `START TRANSACTION` / `BEGIN` … `COMMIT` / `ROLLBACK` statements (three more verified rows in the MySQL report), DDL inside
  the transaction commits it first, as MySQL does.
- `connect(paramstyle="qmark")`: SQLite's own SQL, `%` left alone; the SQLAlchemy dialect uses it (a literal `%%`
  or `%s` in SQLAlchemy's SQL was rewritten before).
- `Connection.getlimit()` for `SQLITE_LIMIT_VARIABLE_NUMBER` / `SQLITE_LIMIT_COLUMN`.
- `sqlite_version` reports the server's engine, 3.50.4 (0.2.0 still said 3.40.0).
- PRAGMAs that read (`table_info`, `foreign_keys`, …) inside a transaction are not replayed.

## 0.2.0 — 2026-10-27

SQLAlchemy and open transactions. Server redeploy required (new `/replay` route); with an old server,
`autocommit=False` raises "redeploy the server" instead of committing anything.

- **SQLAlchemy** (Core and ORM): `create_engine("sqlite+s3rds://user:password@<lambda-url>/<db>")`,
  no extra install. SQLAlchemy's own SQLite dialect, `s3rds.sqlite` as its driver; nothing
  translated. Compared with SQLAlchemy on `sqlite3` in `tests/compat/test_sqlalchemy.py`.
- **`s3rds.sqlite.connect(autocommit=False)`**: sqlite3's transactions — implicit `BEGIN` before
  `INSERT` / `UPDATE` / `DELETE` / `REPLACE`, reads see the transaction's own writes, `commit()` writes S3 once,
  `rollback()` discards, `SAVEPOINT` / `ROLLBACK TO`, `BEGIN` / `COMMIT` / `ROLLBACK` statements,
  `isolation_level` / `in_transaction`. Serializable: another writer committing first makes the transaction
  fail with `OperationalError` ("transaction conflict"). Default `autocommit=True` unchanged.
- **DB-API exception classes** in `s3rds.sqlite`: `IntegrityError`, `OperationalError`, `ProgrammingError`, …
  (subclasses of `s3rds.sqlite.Error`, which still catches everything), chosen as sqlite3 would.
- `CONCAT_WS` works on the Lambda runtime's SQLite (`concat_ws()` needs SQLite 3.44): rewritten to
  `||` / `COALESCE`, NULL arguments skipped, a NULL separator gives NULL, as MySQL. Found by the first run
  against the deployed stack.
- Tests: `python3 -m pytest tests -q --docker run --aws <var-file>` compares MySQL in Docker with the
  deployed Lambda Function URL instead of the in-process handler.
- `executemany` with no parameter sets returns 0 instead of a server error.
- **The server ships its own SQLite 3.50.4** (`sqlean.py==3.50.4.5`, `src/db/requirements.txt`) instead of the
  Lambda runtime's 3.40.0 (the same in python3.12 / 3.13 / 3.14). `scripts/build_server.py` stages `src/db` with
  the Linux wheel; Terraform runs it at plan time (new provider `hashicorp/external`: `terraform init` once),
  CloudFormation packages its output. `GROUP_CONCAT(… ORDER BY … SEPARATOR …)` now verified: MySQL 86% → 87%.
- Exact versions everywhere they are ours: the server engine, the test references (`mysql:8.0.46`,
  SQLAlchemy 2.0.55, sqlglot 30.24.0, sqlean.py 3.50.4.5). The compatibility suite runs only on the pinned
  engine (Python >= 3.10); without it the comparisons are skipped, never run on another SQLite.
- Removed the demo Lambda (`<name>-demo-lambda`, `demo_url`, the `auth_token` variable / `AuthToken`
  parameter) from Terraform and CloudFormation: nobody needed it. The next `apply` deletes it.
- `INSERT` / `UPDATE` / `DELETE … RETURNING` report their rowcount as sqlite3 does (was -1).
- **One install**: `pip install s3rds` brings sqlglot (pinned `>=30.24,<31`, the release the report was verified
  with); the `[mysql]` extra is gone (still accepted, installs nothing more).
- Server: reflection PRAGMAs (`table_info`, `index_list`, `foreign_key_list`, …) no longer re-upload the
  database.

## 0.1.2 — 2026-10-25

MySQL dialect: 59% → 86% of the 99-feature compatibility matrix (`tests/compat/REPORT.md`).
`s3rds.sqlite`: BLOBs. Server redeploy required for typed results and BLOBs; the new server still serves
0.1.1 clients.

- **Result types match pymysql** (`s3rds.pymysql`): `DATETIME` → `datetime`, `DATE` → `date`, `TIME` → `timedelta`,
  `DECIMAL` → `Decimal`, `SUM` / `AVG` / integer division → `Decimal`, `NOW()` / `CURDATE()` / `CURTIME()` typed.
  The server marks values by the column's declared type when the driver asks (`types: true`).
- **BLOB** (both drivers): `bytes` parameters and results travel as `{"$b": base64}`.
- **Strict mode**: `'abc'` into an `INT` / `DECIMAL` column raises 1366 (`DataError`), via a named CHECK.
- **DDL**: inline `KEY` / `INDEX` / `UNIQUE KEY (a, b)` become `CREATE INDEX` (stored as `table__name`),
  `UNSIGNED` / `CHARACTER SET` / `COMMENT` accepted, `ALTER TABLE CHANGE` renames (type-only `MODIFY` is a no-op),
  `RENAME TABLE`, the `DEFAULT` keyword and `INSERT INTO t () VALUES ()`, table-level `CHECK` kept.
- **DML**: `UPDATE … JOIN`, `DELETE t FROM t JOIN …`, `REPLACE INTO` lastrowid, `lastrowid` is 0 for tables
  without `AUTO_INCREMENT` (as MySQL).
- **Functions**: `DATE_ADD` / `DATE_SUB` keep the time, `YEAR` … `SECOND`, `DAYOFWEEK`, `DATEDIFF`, `TIMESTAMPDIFF`,
  `TRUNCATE(x, d)`, `LPAD` / `RPAD`, `REPEAT`, `SUBSTRING_INDEX` (±1), `->` / `->>` / `JSON_UNQUOTE`, `UUID()`, `RAND()`,
  `DATABASE()` inside expressions.
- **Metadata**: `DESCRIBE` / `SHOW COLUMNS` show the declared MySQL types, `UNI` / `MUL` keys;
  `SHOW CREATE TABLE` in MySQL layout; `SHOW INDEX` includes `PRIMARY`; `information_schema.tables` / `columns`;
  `USE db`, `SET NAMES` / `SESSION` / `sql_mode` / `time_zone` accepted; `DROP TABLE` of an unknown table → 1051;
  `CHECK` violations are `OperationalError` 3819 as in pymysql.
- **Refused explicitly** (NotSupportedError): `DROP PROCEDURE`, `SET @x`, `PARTITION BY`.
- Known limits of the Lambda runtime's SQLite 3.40 (see `tests/compat/TASKS.md`, task 22): `GROUP_CONCAT(… ORDER BY …)`,
  `UPDATE` / `DELETE … ORDER BY LIMIT`.

## 0.1.1 — 2026-10-25

`s3rds.sqlite` fixes (inherited by `s3rds.pymysql`). No breaking changes.

- **Parameters**: `datetime`, `date`, `time`, `Decimal`, `bool` and `None` are encoded before sending
  (`'YYYY-MM-DD HH:MM:SS[.ffffff]'`, `'YYYY-MM-DD'`, `'HH:MM:SS'`, decimal as string, bool as 1/0).
  `bytes` and unknown types raise `s3rds.sqlite.Error` naming the parameter, never a bare `TypeError`.
- **`executemany`**: `rowcount` is the sum of rows actually affected; `lastrowid` is the last statement's.
- **No silent truncation**: the server no longer cuts results at 500 rows. Results larger than 4 MB are paged
  (byte-based, under Lambda's 6 MB response limit) with a `next_token` that carries the database version;
  the driver fetches every page inside `execute()`, and if the database changes while paging the server
  refuses to mix versions and the driver re-runs the query (up to 3 times). A result inside `/tx` that
  exceeds a page is an explicit error. A new driver talking to a pre-0.1.1 server raises on `truncated`.
- **`rollback()`**: statements are durable as soon as they run, so `rollback()` cannot undo them; it now
  warns (`RuntimeWarning`) when something was written since the last `commit()`, and stays silent otherwise.

Server-side changes require a redeploy (`terraform apply` / `cloudformation deploy`); the new server still
serves 0.1.0 clients.

## 0.1.0 — 2026-10-25

First release: `s3rds.sqlite` and `s3rds.pymysql` drivers, web console, Terraform and CloudFormation deployments.
