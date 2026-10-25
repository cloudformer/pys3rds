# Tasks

The numbers [REPORT.md](REPORT.md) refers to in its gap notes.

## Open

| task | what |
|---|---|
| 18 | transactions: `autocommit=False` buffers writes, `commit()` runs them as one `/tx`, `rollback()` discards; `START TRANSACTION` / `COMMIT` / `ROLLBACK` statements — **0.2.0** (behaviour change) |
| 22 | Lambda SQLite 3.40 limits: `GROUP_CONCAT(… ORDER BY …)` (needs 3.44) and `UPDATE` / `DELETE … ORDER BY LIMIT` (needs a compile option). Fix by translating to subqueries, or ship a newer SQLite in the Lambda |
| 5 | `strict=True` connection option: refuse any statement the translator does not recognise instead of best-effort |
| 10 | pin the sqlglot version in `client/pyproject.toml` (translations change between releases) |
| 19 | Lambda timeout 15 s → 30 s (terraform + CloudFormation) |
| 20 | GitHub Actions: tests on push, trusted publishing to PyPI on tag — with 0.2.0 |
| 21 | CHANGELOG entries for 0.1.2 / 0.2.0 |

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
| 11 | Lambda runtime's SQLite confirmed: **3.40.0**, math functions yes, `ENABLE_UPDATE_DELETE_LIMIT` no | — |
| 12 | `DESCRIBE` / `SHOW CREATE TABLE` show the declared MySQL types (`_s3rds_columns`) | 0.1.2 |
| 13 | `BLOB`: bytes parameters and results (`{"$b": base64}`) (server + driver) | 0.1.2 |
| 14 | MySQL-only DDL: `UNSIGNED` / `CHARACTER SET` / `COMMENT`, `MODIFY` / `CHANGE`, `RENAME TABLE`, `DEFAULT` keyword | 0.1.2 |
| 15 | `UPDATE … JOIN`, `DELETE t FROM t JOIN …` | 0.1.2 |
| 16 | functions: `YEAR` … `SECOND`, `DAYOFWEEK`, `DATEDIFF`, `TIMESTAMPDIFF`, `LPAD` / `RPAD`, `REPEAT`, `SUBSTRING_INDEX`, `->` / `->>`, `UUID`, `RAND` | 0.1.2 |
| 17 | `information_schema.tables` / `columns`, `DROP TABLE` unknown → 1051, `USE` / `SET …` as no-ops | 0.1.2 |
