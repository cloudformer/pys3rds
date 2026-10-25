# s3rds compatibility report

Generated 2026-10-25 by `pytest tests` · s3rds 0.1.1 · sqlglot 30.24.0 · Python 3.13.7

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match. **verified** = may be listed as supported · **gap** = known difference, named after the task that closes it · **unsupported by design** = s3rds refuses it with NotSupportedError (needs a real server) · **regression** = was verified, now differs.

Whole test run: **76 passed**, 0 failed, 0 expected failures (known gaps), 33 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

## s3rds.pymysql vs MySQL — reference: MySQL (not run)

33 not run

### DDL

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | ALTER TABLE ADD COLUMN / DROP COLUMN |  |
| ⏭ not run | AUTO_INCREMENT never reuses a deleted id |  |
| ⏭ not run | CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062) |  |
| ⏭ not run | CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE |  |
| ⏭ not run | Inline KEY / INDEX in CREATE TABLE creates the index | task 7: inline KEY idx (col) is dropped instead of becoming CREATE INDEX |

### DML

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders |  |
| ⏭ not run | INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col) |  |
| ⏭ not run | ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | task 5: for an upsert that updated an existing row MySQL reports rowcount 2 / lastrowid 0 |
| ⏭ not run | REPLACE INTO |  |
| ⏭ not run | TRUNCATE TABLE |  |

### Queries

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | Case-insensitive string comparison (MySQL default collation) |  |
| ⏭ not run | INNER JOIN and LEFT JOIN |  |
| ⏭ not run | LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) |  |
| ⏭ not run | ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING |  |
| ⏭ not run | Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL |  |
| ⏭ not run | WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR |  |

### Functions

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | ABS, MOD, LOWER, TRIM, REPLACE, LEFT, RIGHT, INSTR |  |
| ⏭ not run | CASE WHEN, COALESCE, NULLIF |  |
| ⏭ not run | DATE_ADD / DATE_SUB with INTERVAL keep the time part | task 6: DATE_ADD drops the time part (DATE() instead of datetime()) |
| ⏭ not run | LAST_INSERT_ID() after an INSERT |  |
| ⏭ not run | String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING |  |

### Constraints & errors

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | ENUM rejects values outside the list with MySQL's error code | task 5: invalid ENUM value is 1265 in MySQL strict mode, 3819 (CHECK) here |
| ⏭ not run | Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL |  |
| ⏭ not run | FOREIGN KEY enforced on insert (1452) |  |
| ⏭ not run | Omitting a NOT NULL column without default raises 1364 | task 5: omitted NOT NULL column is 1364 in MySQL strict mode, 1048 here (SQLite cannot tell the two apart) |

### Metadata

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | DESCRIBE shows MySQL type names | task 12: DESCRIBE shows SQLite type names (text(50)) not MySQL's (varchar(50)) |
| ⏭ not run | SHOW TABLES |  |

### Types

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | Aggregate result types: COUNT(*) int, SUM(INT) Decimal | task 8: SUM() is Decimal in MySQL, int here |
| ⏭ not run | Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int | task 8: DATETIME/DECIMAL come back as str/int, pymysql returns datetime/Decimal |
| ⏭ not run | NOW() / CURDATE() return datetime / date objects | task 9: NOW() returns datetime in pymysql, str here |

### Unsupported

| Status | Feature | Notes |
|---|---|---|
| ⏭ not run | CREATE TRIGGER | stored procedures / triggers run inside the server; use application code |
| ⏭ not run | LOCK TABLES | table locks need a real server |
| ⏭ not run | SELECT ... FOR UPDATE | row locks need a real server; s3rds has one writer and no sessions |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.53.4 (standard library)

8 verified

### DDL

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | CREATE INDEX / UNIQUE INDEX / DROP INDEX, ALTER TABLE ADD / DROP COLUMN, JOIN |  |

### DML

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid |  |

### Queries

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | 1200-row result comes back complete (paging is transparent) |  |
| ✅ verified | GROUP BY aggregates and window functions |  |
| ✅ verified | Subqueries (IN / EXISTS / scalar), UNION, CASE WHEN, COALESCE, json_extract, STRICT type rejection |  |

### Constraints & errors

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax |  |

### Types

| Status | Feature | Notes |
|---|---|---|
| ✅ verified | Parameter binding: int, float, str, None, bool |  |
| ✅ verified | datetime / Decimal parameters stored as canonical text |  |
