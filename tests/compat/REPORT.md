# s3rds compatibility report

Generated 2026-10-25 by `pytest tests` · s3rds 0.1.1 · sqlglot 30.24.0 · Python 3.9.6

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match. The feature list only grows (a removed case would show in git), so the support percentage can only move up.

Whole test run: **157 passed**, 0 failed, 21 expected failures (known gaps), 0 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

## s3rds.pymysql vs MySQL — reference: MySQL 8.0.46 (Docker)

**Support 76%** (76 / 99 features) · 15 gap · 5 quirk · 8 unsupported · 71 verified

✅ verified · ❌ gap — known difference, the note names the task that closes it · ⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run

| Feature | Status | Note |
|---|---|---|
| **DDL** | | |
| ALTER TABLE ADD COLUMN / DROP COLUMN | ✅ |  |
| ALTER TABLE MODIFY COLUMN / CHANGE COLUMN | ✅ |  |
| ALTER TABLE RENAME COLUMN | ✅ |  |
| AUTO_INCREMENT never reuses a deleted id | ✅ |  |
| CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062) | ✅ |  |
| CREATE TABLE ... AS SELECT | ✅ |  |
| CREATE TABLE IF NOT EXISTS / DROP TABLE IF EXISTS | ✅ |  |
| CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE | ✅ |  |
| CREATE VIEW / DROP VIEW | ✅ |  |
| Composite PRIMARY KEY (a, b) | ✅ |  |
| DATE / DATETIME / TIME columns store and compare | ✅ |  |
| DATETIME / TIMESTAMP DEFAULT CURRENT_TIMESTAMP (ON UPDATE accepted) | ✅ |  |
| ENGINE / CHARSET / COLLATE / COMMENT / UNSIGNED options accepted | ✅ |  |
| ENUM column accepts listed values and its DEFAULT | ✅ |  |
| FOREIGN KEY ... ON DELETE CASCADE | ✅ |  |
| Inline KEY / INDEX in CREATE TABLE creates the index | ✅ |  |
| JSON column stores a document | ✅ |  |
| RENAME TABLE / ALTER TABLE ... RENAME TO | ✅ |  |
| UNIQUE column constraint and UNIQUE KEY (a, b) table constraint | ✅ |  |
| BLOB column: bytes parameter in, bytes out | ❌ gap | task 13 |
| DECIMAL(p, s) column keeps its scale and compares exactly | ❌ gap | task 8 |
| **DML** | | |
| CREATE TABLE / INSERT / UPDATE / DELETE / SELECT with %s and %(name)s placeholders | ✅ |  |
| DELETE t FROM t JOIN o ... | ✅ |  |
| INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col) | ✅ |  |
| INSERT ... SELECT | ✅ |  |
| INSERT IGNORE | ✅ |  |
| INSERT with omitted columns and the DEFAULT keyword | ✅ |  |
| Multi-row INSERT: rowcount and lastrowid (first id) | ✅ |  |
| REPLACE INTO | ✅ |  |
| TRUNCATE TABLE | ✅ |  |
| UPDATE / DELETE ... ORDER BY ... LIMIT | ✅ | needs SQLite built with UPDATE/DELETE LIMIT |
| UPDATE t JOIN o ... SET | ✅ |  |
| executemany: rowcount is the total | ✅ |  |
| ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | ⚪ quirk | MySQL 2 / 0, s3rds 1 / row id |
| **Queries** | | |
| 1200-row result comes back complete (paging is transparent) | ✅ |  |
| CROSS JOIN, self join with aliases, RIGHT JOIN | ✅ |  |
| Case-insensitive string comparison (MySQL default collation) | ✅ |  |
| Derived table in FROM, WITH ... AS, WITH RECURSIVE | ✅ |  |
| INNER JOIN and LEFT JOIN | ✅ |  |
| LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) | ✅ |  |
| NULLs sort first ascending and last descending | ✅ |  |
| ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING | ✅ |  |
| Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL | ✅ |  |
| WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR | ✅ |  |
| Window functions: ROW_NUMBER, RANK, COUNT(*) OVER (PARTITION BY) | ✅ |  |
| 7 / 2 is 3.5000 (Decimal) in MySQL, 7 DIV 2 is 3 | ❌ gap | task 8 |
| GROUP_CONCAT with ORDER BY and SEPARATOR | ❌ gap | needs SQLite 3.44+ |
| **Functions** | | |
| ABS, MOD, LOWER, TRIM, REPLACE, LEFT, RIGHT, INSTR | ✅ |  |
| CASE WHEN, COALESCE, NULLIF | ✅ |  |
| CAST(x AS SIGNED / CHAR / DATE), CONVERT | ✅ |  |
| CHAR_LENGTH, LOCATE, LPAD, RPAD, REPEAT, SUBSTRING_INDEX (±1), LTRIM, RTRIM | ✅ |  |
| COUNT, COUNT(DISTINCT), MIN, MAX, SUM, AVG values | ✅ |  |
| DATEDIFF, TIMESTAMPDIFF | ✅ |  |
| DATE_ADD / DATE_SUB with INTERVAL keep the time part | ✅ |  |
| DATE_FORMAT with %Y %m %d %H %i %s | ✅ |  |
| IF(), IFNULL, NULLIF, COALESCE | ✅ |  |
| JSON_EXTRACT, ->, ->>, JSON_UNQUOTE, JSON_OBJECT | ✅ |  |
| LAST_INSERT_ID() after an INSERT | ✅ |  |
| NOW(), CURDATE(), CURTIME(), CURRENT_TIMESTAMP, CURRENT_DATE values | ✅ |  |
| ROUND, FLOOR, CEIL, POWER, SQRT, SIGN, TRUNCATE, GREATEST, LEAST | ✅ |  |
| String functions: IFNULL, CONCAT, CONCAT_WS, UPPER, LENGTH, SUBSTRING | ✅ |  |
| UNIX_TIMESTAMP, FROM_UNIXTIME values | ✅ |  |
| VERSION(), DATABASE(), RAND(), UUID() shape | ✅ |  |
| YEAR, MONTH, DAY, HOUR, MINUTE, SECOND, DAYOFWEEK, DATE(), TIME() | ✅ |  |
| CAST(float AS SIGNED) rounds | ⚪ quirk | MySQL rounds, SQLite truncates |
| ROUND of a float half-way value | ⚪ quirk | MySQL half-to-even, SQLite half-away |
| **Constraints & errors** | | |
| 1050 table already exists, 1051 unknown table on DROP | ✅ |  |
| CHECK constraint violation (3819) | ✅ |  |
| Error classes and MySQL codes: 1062 duplicate, 1146 no table, 1054 no column, 1064 syntax, 1048 NULL | ✅ |  |
| FOREIGN KEY enforced on insert (1452) | ✅ |  |
| Strict mode: 'abc' into an INT column raises 1366 | ❌ gap | task 8 |
| ENUM rejects values outside the list with MySQL's error code | ⚪ quirk | MySQL 1265, s3rds 3819 (CHECK) |
| Omitting a NOT NULL column without default raises 1364 | ⚪ quirk | MySQL 1364, s3rds 1048 |
| **Metadata** | | |
| SHOW DATABASES, SHOW COLUMNS FROM t (field names and keys) | ✅ |  |
| SHOW INDEX FROM t (key and column names) | ✅ |  |
| SHOW TABLES | ✅ |  |
| USE db, SET NAMES, SET SESSION sql_mode are accepted | ✅ |  |
| information_schema.tables / columns | ✅ |  |
| DESCRIBE shows MySQL type names | ❌ gap | task 12 |
| SHOW CREATE TABLE returns the original column definitions | ❌ gap | task 12 |
| **Types** | | |
| Parameter binding: int, float, str, None, bool | ✅ |  |
| Result types: INT → int, VARCHAR → str, DOUBLE / FLOAT → float, NULL → None | ✅ |  |
| datetime / date / Decimal parameters | ✅ |  |
| Aggregate result types: COUNT(*) int, SUM(INT) Decimal | ❌ gap | task 8 |
| BLOB results come back as bytes | ❌ gap | task 13 |
| NOW() / CURDATE() return datetime / date objects | ❌ gap | task 9 |
| Result types: DATETIME → datetime, DATE → date, TIME → timedelta | ❌ gap | task 8 |
| Result types: DECIMAL → Decimal, TINYINT(1) → int, BIGINT → int | ❌ gap | task 8 |
| **Unsupported** | | |
| CREATE PROCEDURE / CALL | ⛔ | server-side code; use the application |
| CREATE TRIGGER | ⛔ | server-side code; use the application |
| CREATE USER / GRANT | ⛔ | users are the endpoint credentials |
| FULLTEXT index, MATCH ... AGAINST | ⛔ | full-text search needs a real server |
| LOCK TABLES | ⛔ | table locks need a real server |
| PARTITION BY | ⛔ | one file per database |
| SELECT ... FOR UPDATE | ⛔ | row locks need a real server |
| SET @x := 1 user variables | ⛔ | no session state between requests |
| **Transactions** | | |
| DDL inside a transaction commits implicitly (MySQL semantics) | ❌ gap | task 18 |
| START TRANSACTION ... COMMIT | ❌ gap | task 18 |
| START TRANSACTION ... ROLLBACK discards the writes | ❌ gap | task 18 |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.43.2 (standard library)

**Support 88%** (8 / 9 features) · 1 gap · 8 verified

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
| BLOB values round-trip as bytes | ❌ gap | task 13 |
