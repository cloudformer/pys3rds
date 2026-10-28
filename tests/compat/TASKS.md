# Tasks

The numbers [REPORT.md](REPORT.md) refers to in its gap notes.

## Open

| task | what |
|---|---|
| 18 | `s3rds.pymysql` transactions: `autocommit=False` and `START TRANSACTION` / `COMMIT` / `ROLLBACK` statements, on the open transactions of task 23 (pymysql opens one on any statement, sqlite3 only on writes) — **0.2.0** |
| 23 | `s3rds.sqlite` `autocommit=False` (open transactions replayed on the server, `/replay`) and SQLAlchemy `sqlite+s3rds://` (`tests/compat/test_sqlalchemy.py`) — **0.2.0**, done in code, waiting for the first full run |
| 24 | Django: a database backend on `s3rds.sqlite`; its SQLite backend registers ~40 Python functions on the connection, which would have to exist on the server — evaluate before starting |
| 22 | `UPDATE` / `DELETE … ORDER BY LIMIT`: the server's SQLite (sqlean.py 3.50.4) is built without ENABLE_UPDATE_DELETE_LIMIT. Fix by translating to a subquery on the rowid. (`GROUP_CONCAT(… ORDER BY …)` works since the server ships SQLite 3.50.4.) |
| 5 | `strict=True` connection option: refuse any statement the translator does not recognise instead of best-effort |
| 19 | Lambda timeout 15 s → 30 s (terraform + CloudFormation) |
| 21 | CHANGELOG entry for 0.2.0 |

Permanent, not tasks: ⚪ quirks (documented MySQL behaviour we do not imitate) and ⛔ unsupported
(row / table locks, triggers, procedures, user variables, FULLTEXT, users / GRANT, partitions).

## Done

| task | what | release |
|---|---|---|
| 1 | `s3rds.sqlite`: parameter encoding (datetime / Decimal / bool), unknown types raise | 0.1.1 |
| 2 | `s3rds.sqlite`: results paged by bytes, the driver fetches every page, version change → re-run | 0.1.1 |
| 3 | `s3rds.sqlite`: `executemany` rowcount, strict retry policy (never duplicate a write) | 0.1.1 |
| 4 | `s3rds.sqlite`: `rollback()` warns when writes already landed since the last `commit()` | 0.1.1 |
| 6 | `DATE_ADD` / `DATE_SUB` keep the time part | 0.1.2 |
| 7 | inline `KEY` / `UNIQUE KEY (a, b)` → `CREATE INDEX` in the same transaction | 0.1.2 |
| 8 | result types: `DATETIME` / `DATE` / `TIME` / `DECIMAL` columns, `SUM` / `AVG` / division, strict-mode 1366 (server + driver) | 0.1.2 |
| 9 | `NOW()` / `CURDATE()` / `CURTIME()` return `datetime` / `date` / `timedelta` | 0.1.2 |
| 10 | sqlglot pinned `>=30.24,<31` (the release the report was verified with), a dependency of `pip install s3rds` | 0.2.0 |
| 11 | Lambda runtime's SQLite confirmed: **3.40.0**, math functions yes, `ENABLE_UPDATE_DELETE_LIMIT` no | — |
| 12 | `DESCRIBE` / `SHOW CREATE TABLE` show the declared MySQL types (`_s3rds_columns`) | 0.1.2 |
| 13 | `BLOB`: bytes parameters and results (`{"$b": base64}`) (server + driver) | 0.1.2 |
| 14 | MySQL-only DDL: `UNSIGNED` / `CHARACTER SET` / `COMMENT`, `MODIFY` / `CHANGE`, `RENAME TABLE`, `DEFAULT` keyword | 0.1.2 |
| 15 | `UPDATE … JOIN`, `DELETE t FROM t JOIN …` | 0.1.2 |
| 16 | functions: `YEAR` … `SECOND`, `DAYOFWEEK`, `DATEDIFF`, `TIMESTAMPDIFF`, `LPAD` / `RPAD`, `REPEAT`, `SUBSTRING_INDEX`, `->` / `->>`, `UUID`, `RAND` | 0.1.2 |
| 17 | `information_schema.tables` / `columns`, `DROP TABLE` unknown → 1051, `USE` / `SET …` as no-ops | 0.1.2 |
| 20 | GitHub Actions: tests (with MySQL) on push, trusted publishing to PyPI on `v*` tags | — |
