# Changelog

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
