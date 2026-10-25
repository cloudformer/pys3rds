# s3rds compatibility report

Generated 2026-10-25 by `pytest tests` · s3rds 0.1.1 · sqlglot 30.24.0 · Python 3.9.6

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match.

Whole test run: **102 passed**, 0 failed, 9 expected failures (known gaps), 0 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

## s3rds.pymysql vs MySQL — reference: MySQL 8.0.46 (Docker)

6 gap · 3 quirk · 3 unsupported · 21 verified

✅ verified · ❌ gap — known difference, the note names the task that closes it · ⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run

| Feature | Status | Note |
|---|---|---|
| **DDL** | | |
| ALTER TABLE ADD COLUMN / DROP COLUMN | ✅ |  |
| AUTO_INCREMENT never reuses a deleted id | ✅ |  |
| CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062) | ✅ |  |
| CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE | ✅ |  |
| Inline KEY / INDEX in CREATE TABLE creates the index | ❌ gap | task 7 |
| **DML** | | |
| CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders | ✅ |  |
| INSERT IGNORE and INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col) | ✅ |  |
| REPLACE INTO | ✅ |  |
| TRUNCATE TABLE | ✅ |  |
| ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | ⚪ quirk | MySQL 2 / 0, s3rds 1 / row id |
| **Queries** | | |
| Case-insensitive string comparison (MySQL default collation) | ✅ |  |
| INNER JOIN and LEFT JOIN | ✅ |  |
| LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) | ✅ |  |
| ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING | ✅ |  |
| Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL | ✅ |  |
| WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR | ✅ |  |
| **Functions** | | |
| ABS, MOD, LOWER, TRIM, REPLACE, LEFT, RIGHT, INSTR | ✅ |  |
| CASE WHEN, COALESCE, NULLIF | ✅ |  |
| LAST_INSERT_ID() after an INSERT | ✅ |  |
| String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING | ✅ |  |
| DATE_ADD / DATE_SUB with INTERVAL keep the time part | ❌ gap | task 6 |
| **Constraints & errors** | | |
| Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL | ✅ |  |
| FOREIGN KEY enforced on insert (1452) | ✅ |  |
| ENUM rejects values outside the list with MySQL's error code | ⚪ quirk | MySQL 1265, s3rds 3819 (CHECK) |
| Omitting a NOT NULL column without default raises 1364 | ⚪ quirk | MySQL 1364, s3rds 1048 |
| **Metadata** | | |
| SHOW TABLES | ✅ |  |
| DESCRIBE shows MySQL type names | ❌ gap | task 12 |
| **Types** | | |
| Aggregate result types: COUNT(*) int, SUM(INT) Decimal | ❌ gap | task 8 |
| Column types on results: DATETIME → datetime, DECIMAL → Decimal, TINYINT(1) → int | ❌ gap | task 8 |
| NOW() / CURDATE() return datetime / date objects | ❌ gap | task 9 |
| **Unsupported** | | |
| CREATE TRIGGER | ⛔ | server-side code; use the application |
| LOCK TABLES | ⛔ | table locks need a real server |
| SELECT ... FOR UPDATE | ⛔ | row locks need a real server |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.43.2 (standard library)

8 verified

✅ verified · ❌ gap — known difference, the note names the task that closes it · ⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run

| Feature | Status | Note |
|---|---|---|
| **DDL** | | |
| CREATE INDEX / UNIQUE INDEX / DROP INDEX, ALTER TABLE ADD / DROP COLUMN, JOIN | ✅ |  |
| **DML** | | |
| CREATE TABLE / INSERT / UPDATE / DELETE / SELECT, rowcount and lastrowid | ✅ |  |
| **Queries** | | |
| 1200-row result comes back complete (paging is transparent) | ✅ |  |
| GROUP BY aggregates and window functions | ✅ |  |
| Subqueries (IN / EXISTS / scalar), UNION, CASE WHEN, COALESCE, json_extract, STRICT type rejection | ✅ |  |
| **Constraints & errors** | | |
| Errors are raised, never swallowed: UNIQUE, NOT NULL, no such table, syntax | ✅ |  |
| **Types** | | |
| Parameter binding: int, float, str, None, bool | ✅ |  |
| datetime / Decimal parameters stored as canonical text | ✅ |  |
