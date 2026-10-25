# s3rds compatibility report

Generated 2026-10-25 by `pytest tests` · s3rds 0.1.1 · sqlglot 30.24.0 · Python 3.9.6

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match. **verified** = may be listed as supported · **gap** = known difference, named after the task that closes it · **unsupported by design** = s3rds refuses it with NotSupportedError (needs a real server) · **engine quirk** = MySQL-internal behaviour (e.g. AUTO_INCREMENT values burnt by failed inserts) that is documented and deliberately not imitated · **regression** = was verified, now differs.

Whole test run: **102 passed**, 0 failed, 9 expected failures (known gaps), 0 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

## s3rds.pymysql vs MySQL — reference: MySQL 8.0.46 (Docker)

6 gap · 3 quirk · 3 unsupported · 21 verified

| Category | Status | Feature | Notes |
|---|---|---|---|
| DDL | ✅ verified | ALTER TABLE ADD COLUMN / DROP COLUMN |  |
| DDL | ✅ verified | AUTO_INCREMENT never reuses a deleted id |  |
| DDL | ✅ verified | CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062) |  |
| DDL | ✅ verified | CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE |  |
| DDL | ❌ gap | Inline KEY / INDEX in CREATE TABLE creates the index | task 7: inline KEY idx (col) is dropped instead of becoming CREATE INDEX |
| DML | ✅ verified | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders |  |
| DML | ✅ verified | INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col) |  |
| DML | ✅ verified | REPLACE INTO |  |
| DML | ✅ verified | TRUNCATE TABLE |  |
| DML | ⚪ engine quirk (documented, not aligned) | ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | for an upsert that updated an existing row MySQL reports rowcount 2 / lastrowid 0; s3rds reports 1 / the row id |
| Queries | ✅ verified | Case-insensitive string comparison (MySQL default collation) |  |
| Queries | ✅ verified | INNER JOIN and LEFT JOIN |  |
| Queries | ✅ verified | LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) |  |
| Queries | ✅ verified | ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING |  |
| Queries | ✅ verified | Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL |  |
| Queries | ✅ verified | WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR |  |
| Functions | ✅ verified | ABS, MOD, LOWER, TRIM, REPLACE, LEFT, RIGHT, INSTR |  |
| Functions | ✅ verified | CASE WHEN, COALESCE, NULLIF |  |
| Functions | ✅ verified | LAST_INSERT_ID() after an INSERT |  |
| Functions | ✅ verified | String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING |  |
| Functions | ❌ gap | DATE_ADD / DATE_SUB with INTERVAL keep the time part | task 6: DATE_ADD drops the time part (DATE() instead of datetime()) |
| Constraints & errors | ✅ verified | Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL |  |
| Constraints & errors | ✅ verified | FOREIGN KEY enforced on insert (1452) |  |
| Constraints & errors | ⚪ engine quirk (documented, not aligned) | ENUM rejects values outside the list with MySQL's error code | MySQL strict mode reports 1265 for a bad ENUM value; s3rds enforces it with a CHECK constraint (3819) |
| Constraints & errors | ⚪ engine quirk (documented, not aligned) | Omitting a NOT NULL column without default raises 1364 | MySQL distinguishes an omitted NOT NULL column (1364) from an explicit NULL (1048); SQLite cannot, s3rds reports 1048 |
| Metadata | ✅ verified | SHOW TABLES |  |
| Metadata | ❌ gap | DESCRIBE shows MySQL type names | task 12: DESCRIBE shows SQLite type names (text(50)) not MySQL's (varchar(50)) |
| Types | ❌ gap | Aggregate result types: COUNT(*) int, SUM(INT) Decimal | task 8: SUM() is Decimal in MySQL, int here |
| Types | ❌ gap | Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int | task 8: DATETIME/DECIMAL come back as str/int, pymysql returns datetime/Decimal |
| Types | ❌ gap | NOW() / CURDATE() return datetime / date objects | task 9: NOW() returns datetime in pymysql, str here |
| Unsupported | ⛔ unsupported by design | CREATE TRIGGER | stored procedures / triggers run inside the server; use application code |
| Unsupported | ⛔ unsupported by design | LOCK TABLES | table locks need a real server |
| Unsupported | ⛔ unsupported by design | SELECT ... FOR UPDATE | row locks need a real server; s3rds has one writer and no sessions |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.43.2 (standard library)

8 verified

| Category | Status | Feature | Notes |
|---|---|---|---|
| DDL | ✅ verified | CREATE INDEX / UNIQUE INDEX / DROP INDEX, ALTER TABLE ADD / DROP COLUMN, JOIN |  |
| DML | ✅ verified | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid |  |
| Queries | ✅ verified | 1200-row result comes back complete (paging is transparent) |  |
| Queries | ✅ verified | GROUP BY aggregates and window functions |  |
| Queries | ✅ verified | Subqueries (IN / EXISTS / scalar), UNION, CASE WHEN, COALESCE, json_extract, STRICT type rejection |  |
| Constraints & errors | ✅ verified | Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax |  |
| Types | ✅ verified | Parameter binding: int, float, str, None, bool |  |
| Types | ✅ verified | datetime / Decimal parameters stored as canonical text |  |
