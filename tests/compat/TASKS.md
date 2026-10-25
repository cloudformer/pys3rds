# Tasks

The numbers [REPORT.md](REPORT.md) refers to in its gap notes. Done tasks stay so old notes keep resolving.

| task | what | status |
|---|---|---|
| 5 | `strict=True` connection option: refuse any statement the translator does not recognise instead of best-effort | open |
| 6 | `DATE_ADD` / `DATE_SUB` keep the time part | done |
| 7 | inline `KEY` / `UNIQUE KEY (a, b)` → `CREATE INDEX` in the same transaction | done |
| 8 | result types: `DATETIME` / `DATE` / `TIME` / `DECIMAL` columns, `SUM` / `AVG` / division, strict-mode 1366 | done (server + driver) |
| 9 | `NOW()` / `CURDATE()` / `CURTIME()` return `datetime` / `date` / `timedelta` | done |
| 10 | pin the sqlglot version in `client/pyproject.toml` (translations change between releases) | open |
| 11 | confirm the Lambda runtime's SQLite: **3.40.0**, math functions yes, `ENABLE_UPDATE_DELETE_LIMIT` no | done |
| 12 | `DESCRIBE` / `SHOW CREATE TABLE` show the declared MySQL types (`_s3rds_columns`) | done |
| 13 | `BLOB`: bytes parameters and results (`{"$b": base64}`) | done (server + driver) |
| 14 | MySQL-only DDL: `UNSIGNED` / `CHARACTER SET` / `COMMENT`, `MODIFY` / `CHANGE`, `RENAME TABLE`, `DEFAULT` keyword | done |
| 15 | `UPDATE … JOIN`, `DELETE t FROM t JOIN …` | done |
| 16 | functions: `YEAR` … `SECOND`, `DAYOFWEEK`, `DATEDIFF`, `TIMESTAMPDIFF`, `LPAD` / `RPAD`, `REPEAT`, `SUBSTRING_INDEX`, `->` / `->>`, `UUID`, `RAND` | done |
| 17 | `information_schema.tables` / `columns`, `DROP TABLE` unknown → 1051, `USE` / `SET …` as no-ops | done |
| 18 | transactions: `autocommit=False` buffers writes, `commit()` runs them as one `/tx`, `rollback()` discards; `START TRANSACTION` / `COMMIT` / `ROLLBACK` statements | open — 0.2.0 (behaviour change) |
| 19 | Lambda timeout 15 s → 30 s (terraform + CloudFormation) | open |
| 20 | GitHub Actions: tests on push, trusted publishing to PyPI on tag | open — with 0.2.0 |
| 21 | CHANGELOG entries for 0.1.2 / 0.2.0 | open |
| 22 | Lambda SQLite 3.40 limits: `GROUP_CONCAT(… ORDER BY …)` (needs 3.44) and `UPDATE` / `DELETE … ORDER BY LIMIT` (needs a compile option). Fix by translating to subqueries, or ship a newer SQLite in the Lambda | open |

Permanent, not tasks: ⚪ quirks (documented MySQL behaviour we do not imitate) and ⛔ unsupported
(row / table locks, triggers, procedures, user variables, FULLTEXT, users / GRANT, partitions).
