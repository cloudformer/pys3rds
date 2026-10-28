# s3rds compatibility report · [tasks](TASKS.md)

Generated 2026-10-27 by `pytest tests` · s3rds 0.2.0 · sqlglot 30.24.0 · Python 3.13.7

Each row is one feature exercised with the same SQL on the reference database and on s3rds; results, Python types, rowcount, lastrowid and error codes must match. The feature list only grows (a removed case would show in git), so the support percentage can only move up.

Whole test run: **207 passed**, 0 failed, 8 expected failures (known gaps), 1 skipped — regression suite (`tests/`) and compatibility suite (`tests/compat/`) together.

Open tasks: task 18 (3), task 22 (1) — see [TASKS.md](TASKS.md).

## s3rds.pymysql (deployed Lambda https://fv6zi4zg3qnt6q4n2yitsq3lza0refke.lambda-url.us-east-2.on.aws/) vs MySQL — reference: MySQL 8.0.46 (Docker)

**Support 87%** (87 / 99 features) · 4 gap · 5 quirk · 8 unsupported · 82 verified

✅ verified · ❌ gap — known difference, the note names the task that closes it · ⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run

| Feature | Status | Note |
|---|---|---|
| **DDL** | | |
| ALTER TABLE ADD COLUMN / DROP COLUMN | ✅ |  |
| ALTER TABLE MODIFY COLUMN / CHANGE COLUMN | ✅ |  |
| ALTER TABLE RENAME COLUMN | ✅ |  |
| AUTO_INCREMENT never reuses a deleted id | ✅ |  |
| BLOB column: bytes parameter in, bytes out | ✅ |  |
| CREATE INDEX / CREATE UNIQUE INDEX / DROP INDEX ... ON t; unique index enforced (1062) | ✅ |  |
| CREATE TABLE ... AS SELECT | ✅ |  |
| CREATE TABLE IF NOT EXISTS / DROP TABLE IF EXISTS | ✅ |  |
| CREATE TABLE with INT, BIGINT, VARCHAR, TEXT, BOOLEAN, DEFAULT and NOT NULL; DROP TABLE | ✅ |  |
| CREATE VIEW / DROP VIEW | ✅ |  |
| Composite PRIMARY KEY (a, b) | ✅ |  |
| DATE / DATETIME / TIME columns store and compare | ✅ |  |
| DATETIME / TIMESTAMP DEFAULT CURRENT_TIMESTAMP (ON UPDATE accepted) | ✅ |  |
| DECIMAL(p, s) column keeps its scale and compares exactly | ✅ |  |
| ENGINE / CHARSET / COLLATE / COMMENT / UNSIGNED options accepted | ✅ |  |
| ENUM column accepts listed values and its DEFAULT | ✅ |  |
| FOREIGN KEY ... ON DELETE CASCADE | ✅ |  |
| Inline KEY / INDEX in CREATE TABLE creates the index | ✅ |  |
| JSON column stores a document | ✅ |  |
| RENAME TABLE / ALTER TABLE ... RENAME TO | ✅ |  |
| UNIQUE column constraint and UNIQUE KEY (a, b) table constraint | ✅ |  |
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
| UPDATE t JOIN o ... SET | ✅ |  |
| executemany: rowcount is the total | ✅ |  |
| UPDATE / DELETE ... ORDER BY ... LIMIT | ❌ gap | task 22 |
| ON DUPLICATE KEY UPDATE reports rowcount 2 and lastrowid 0 when it updates | ⚪ quirk | MySQL 2 / 0, s3rds 1 / row id |
| **Queries** | | |
| 1200-row result comes back complete (paging is transparent) | ✅ |  |
| 7 / 2 is 3.5000 (Decimal) in MySQL, 7 DIV 2 is 3 | ✅ |  |
| CROSS JOIN, self join with aliases, RIGHT JOIN | ✅ |  |
| Case-insensitive string comparison (MySQL default collation) | ✅ |  |
| Derived table in FROM, WITH ... AS, WITH RECURSIVE | ✅ |  |
| GROUP_CONCAT with ORDER BY and SEPARATOR | ✅ | task 22 |
| INNER JOIN and LEFT JOIN | ✅ |  |
| LIMIT offset, count and LIMIT ... OFFSET; multi-row INSERT lastrowid (first id) | ✅ |  |
| NULLs sort first ascending and last descending | ✅ |  |
| ORDER BY, DISTINCT, GROUP BY with COUNT, HAVING | ✅ |  |
| Subquery in WHERE (IN / EXISTS), scalar subquery, UNION / UNION ALL | ✅ |  |
| WHERE with LIKE, IN, BETWEEN, IS NULL, <>, AND / OR | ✅ |  |
| Window functions: ROW_NUMBER, RANK, COUNT(*) OVER (PARTITION BY) | ✅ |  |
| **Transactions** | | |
| DDL inside a transaction commits implicitly (MySQL semantics) | ❌ gap | task 18 |
| START TRANSACTION ... COMMIT | ❌ gap | task 18 |
| START TRANSACTION ... ROLLBACK discards the writes | ❌ gap | task 18 |
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
| Strict mode: 'abc' into an INT column raises 1366 | ✅ |  |
| ENUM rejects values outside the list with MySQL's error code | ⚪ quirk | MySQL 1265, s3rds 3819 (CHECK) |
| Omitting a NOT NULL column without default raises 1364 | ⚪ quirk | MySQL 1364, s3rds 1048 |
| **Metadata** | | |
| DESCRIBE shows MySQL type names | ✅ |  |
| SHOW CREATE TABLE returns the original column definitions | ✅ |  |
| SHOW DATABASES, SHOW COLUMNS FROM t (field names and keys) | ✅ |  |
| SHOW INDEX FROM t (key and column names) | ✅ |  |
| SHOW TABLES | ✅ |  |
| USE db, SET NAMES, SET SESSION sql_mode are accepted | ✅ |  |
| information_schema.tables / columns | ✅ |  |
| **Types** | | |
| Aggregate result types: COUNT(*) int, SUM(INT) Decimal | ✅ |  |
| BLOB results come back as bytes | ✅ |  |
| NOW() / CURDATE() return datetime / date objects | ✅ |  |
| Parameter binding: int, float, str, None, bool | ✅ |  |
| Result types: DATETIME → datetime, DATE → date, TIME → timedelta | ✅ |  |
| Result types: DECIMAL → Decimal, TINYINT(1) → int, BIGINT → int | ✅ |  |
| Result types: INT → int, VARCHAR → str, DOUBLE / FLOAT → float, NULL → None | ✅ |  |
| datetime / date / Decimal parameters | ✅ |  |
| **Unsupported** | | |
| CREATE PROCEDURE / CALL | ⛔ | server-side code; use the application |
| CREATE TRIGGER | ⛔ | server-side code; use the application |
| CREATE USER / GRANT | ⛔ | users are the endpoint credentials |
| FULLTEXT index, MATCH ... AGAINST | ⛔ | full-text search needs a real server |
| LOCK TABLES | ⛔ | table locks need a real server |
| PARTITION BY | ⛔ | one file per database |
| SELECT ... FOR UPDATE | ⛔ | row locks need a real server |
| SET @x := 1 user variables | ⛔ | no session state between requests |

## s3rds.sqlite vs sqlite3 — reference: SQLite 3.50.4 (sqlean.py 3.50.4.5)

**Support 100%** (9 / 9 features) · 9 verified

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
| BLOB values round-trip as bytes | ✅ |  |
| Parameter binding: int, float, str, None, bool | ✅ |  |
| datetime / Decimal parameters stored as canonical text | ✅ |  |

## SQLAlchemy sqlite+s3rds vs SQLAlchemy on sqlite3 — reference: SQLAlchemy 2.0.55 on SQLite 3.50.4 (sqlean.py 3.50.4.5)

**Support 100%** (14 / 14 features) · 14 verified

✅ verified · ❌ gap — known difference, the note names the task that closes it · ⚪ engine quirk — documented, deliberately not aligned · ⛔ unsupported by design, refused with NotSupportedError · 🟡 fixed — xfail marker to remove · 🔴 regression · ⏭ not run

| Feature | Status | Note |
|---|---|---|
| **DML** | | |
| Core: create_all, insert (executemany), update / delete rowcount, select with where / order by / limit | ✅ |  |
| INSERT … RETURNING for one and many rows; ids continue after a delete | ✅ |  |
| **Queries** | | |
| join, group by / having, subquery, distinct, coalesce / lower, offset | ✅ |  |
| **ORM** | | |
| Session add / commit / query, autoincrement ids, relationship load, cascade delete-orphan | ✅ |  |
| bulk update / delete statements with session sync, Session.get, Session.merge | ✅ |  |
| joined-table inheritance: one object, two tables; polymorphic queries return the subclass | ✅ |  |
| many-to-many through an association table; joinedload; removing a link deletes only the link row | ✅ |  |
| version_id_col: the second of two concurrent updates of one row raises StaleDataError | ✅ |  |
| **Transactions** | | |
| Session.rollback() and an exception inside engine.begin() discard the writes, flushed ones included | ✅ |  |
| autoflush: a query sees the session's own uncommitted writes; another client sees them only after commit | ✅ |  |
| begin_nested (SAVEPOINT): rolling back the inner block keeps the outer writes | ✅ |  |
| **Constraints & errors** | | |
| UNIQUE and FOREIGN KEY violations are sqlalchemy.exc.IntegrityError; the session works again after rollback | ✅ |  |
| **Metadata** | | |
| inspect(): table names, columns, primary key, foreign keys, indexes, unique constraints | ✅ |  |
| **Types** | | |
| DateTime, Date, Numeric, Float, Boolean, LargeBinary, JSON, Enum round-trip with their Python types | ✅ |  |
