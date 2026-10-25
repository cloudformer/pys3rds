# s3rds compatibility report

Generated 2026-10-25 by `pytest tests` · s3rds 0.1.1 · sqlglot 30.24.0 · Python 3.9.6

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match. **verified** = may be listed as supported · **gap** = known difference, named after the task that closes it · **regression** = was verified, now differs.

Whole test run: **82 passed**, 0 failed, 9 expected failures (known gaps), 0 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

## s3rds.pymysql vs MySQL — reference: MySQL 8.0.46 (Docker)

9 gap · 8 verified

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders |  |
| ✅ verified | Case-insensitive string comparison (MySQL default collation) |  |
| ✅ verified | Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL |  |
| ✅ verified | INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col) |  |
| ✅ verified | LAST_INSERT_ID() after an INSERT |  |
| ✅ verified | LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) |  |
| ✅ verified | SHOW TABLES |  |
| ✅ verified | String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING |  |
| ❌ gap | Aggregate result types: COUNT(*) int, SUM(INT) Decimal | task 8: SUM() is Decimal in MySQL, int here |
| ❌ gap | Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int | task 8: DATETIME/DECIMAL come back as str/int, pymysql returns datetime/Decimal |
| ❌ gap | DATE_ADD / DATE_SUB with INTERVAL keep the time part | task 6: DATE_ADD drops the time part (DATE() instead of datetime()) |
| ❌ gap | DESCRIBE shows MySQL type names | task 12: DESCRIBE shows SQLite type names (text(50)) not MySQL's (varchar(50)) |
| ❌ gap | ENUM rejects values outside the list with MySQL's error code | task 5: invalid ENUM value is 1265 in MySQL strict mode, 3819 (CHECK) here |
| ❌ gap | Inline KEY / INDEX in CREATE TABLE creates the index | task 7: inline KEY idx (col) is dropped instead of becoming CREATE INDEX |
| ❌ gap | NOW() / CURDATE() return datetime / date objects | task 9: NOW() returns datetime in pymysql, str here |
| ❌ gap | ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | task 5: for an upsert that updated an existing row MySQL reports rowcount 2 / lastrowid 0 |
| ❌ gap | Omitting a NOT NULL column without default raises 1364 | task 5: omitted NOT NULL column is 1364 in MySQL strict mode, 1048 here (SQLite cannot tell the two apart) |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.43.2 (standard library)

6 verified

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | 1200-row result comes back complete (paging is transparent) |  |
| ✅ verified | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid |  |
| ✅ verified | Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax |  |
| ✅ verified | GROUP BY aggregates and window functions |  |
| ✅ verified | Parameter binding: int, float, str, None, bool |  |
| ✅ verified | datetime / Decimal parameters stored as canonical text |  |
