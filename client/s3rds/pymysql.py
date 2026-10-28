"""s3rds.pymysql — the MySQL-dialect driver for s3rds. Write MySQL; it runs on the s3rds endpoint (SQLite engine).

    pip install s3rds                      # sqlglot (the dialect translation) comes with it

    from s3rds import pymysql              # later: `import pymysql` on a real RDS, nothing else changes
    conn = pymysql.connect(host="xxx.lambda-url.us-east-2.on.aws",
                           user="admin", password="...", database="blog")
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50) NOT NULL)")
        cur.execute("INSERT INTO users (name) VALUES (%s)", ["alice"])
        cur.execute("INSERT INTO users (name) VALUES (%(n)s)", {"n": "bob"})
        cur.execute("SELECT * FROM users WHERE name = %s LIMIT 0, 10", ["ALICE"])   # case-insensitive, like MySQL

What is translated (client side, cached per statement):
  - placeholders %s and %(name)s, %% -> %           - backticks, LIMIT offset,count
  - AUTO_INCREMENT -> INTEGER PRIMARY KEY AUTOINCREMENT   - ENUM(...) -> TEXT CHECK (col IN (...))
  - text columns get COLLATE NOCASE (MySQL's default case-insensitive comparison)
  - INSERT IGNORE, ON DUPLICATE KEY UPDATE (VALUES(col) -> excluded.col), REPLACE INTO, the DEFAULT keyword
  - inline KEY / INDEX / UNIQUE KEY -> CREATE INDEX (stored as table__name), RENAME TABLE, CHANGE COLUMN (rename)
  - UPDATE ... JOIN -> UPDATE ... FROM, DELETE t FROM t JOIN ... -> DELETE ... WHERE EXISTS
  - table options (ENGINE=, CHARSET=, COMMENT), ON UPDATE CURRENT_TIMESTAMP are dropped; UNSIGNED -> signed
  - functions sqlglot knows (IFNULL, CONCAT_WS, DATE_FORMAT, ...) plus the _FUNCS table below
  - SHOW TABLES / DATABASES / COLUMNS / CREATE TABLE / INDEX, DESCRIBE, information_schema.tables / columns,
    SELECT VERSION(), SELECT DATABASE(); USE and SET NAMES / SESSION / ... are accepted as no-ops
  - errors come back as pymysql-style classes with MySQL codes: 1062, 1146, 1054, 1064, 1048, 1452, ...

Not supported, raised as NotSupportedError (never silently wrong): stored procedures/functions,
triggers, FULLTEXT, SELECT ... FOR UPDATE, LOCK TABLES, GRANT/REVOKE/users.
Result types follow pymysql: DATETIME/DATE/TIME/DECIMAL columns (marked by the server from the declared
type), SUM/AVG/division as Decimal, NOW() as datetime, BLOB as bytes. MySQL column types are kept in the
_s3rds_columns table so DESCRIBE / SHOW CREATE TABLE show what was declared.
"""
import datetime
import decimal
import functools
import re

from . import sqlite as _sqlite
from .sqlite import Cursor, DictCursor, cursors  # noqa: F401  (same DB-API cursors)

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError
except ImportError:  # the error surfaces on first query, with install instructions
    sqlglot = None

__version__ = _sqlite.__version__
apilevel = "2.0"
threadsafety = 1
paramstyle = "pyformat"


# ---------------- exceptions (pymysql hierarchy; args[0] = MySQL error code) ----------------

class Error(Exception):
    def __init__(self, code, message):
        super().__init__(code, message)
        self.code = code
        self.message = message

    def __str__(self):
        return f"({self.code}, {self.message!r})"


class InterfaceError(Error): pass
class DatabaseError(Error): pass
class DataError(DatabaseError): pass
class OperationalError(DatabaseError): pass
class IntegrityError(DatabaseError): pass
class InternalError(DatabaseError): pass
class ProgrammingError(DatabaseError): pass
class NotSupportedError(DatabaseError): pass


_SQLITE_TO_MYSQL = [
    (r"UNIQUE constraint failed",      IntegrityError,   1062, "Duplicate entry"),
    (r"NOT NULL constraint failed",    IntegrityError,   1048, "Column cannot be null"),
    (r"FOREIGN KEY constraint failed", IntegrityError,   1452, "Cannot add or update a child row: a foreign key constraint fails"),
    (r"CHECK constraint failed: s3rds_type", DataError, 1366, "Incorrect value for column"),  # our typeof() checks
    (r"CHECK constraint failed",       OperationalError, 3819, "Check constraint is violated"),  # pymysql: OperationalError
    (r"no such table",                 ProgrammingError, 1146, "Table doesn't exist"),
    (r"no such column",                OperationalError, 1054, "Unknown column"),  # pymysql: OperationalError
    (r"already exists",                OperationalError, 1050, "Table already exists"),
    (r"no such function",              ProgrammingError, 1305, "FUNCTION does not exist"),
    (r"syntax error|incomplete input", ProgrammingError, 1064, "You have an error in your SQL syntax"),
    (r"readonly|read-only",            OperationalError, 1290, "The server is running in read-only mode"),
    (r"cannot store .* value in .* column", DataError, 1366, "Incorrect value for column"),
]


def _map_error(e, sql=""):
    """s3rds.sqlite.Error (HTTP status + server message) -> pymysql-style exception with a MySQL code."""
    msg = e.message or str(e)
    if re.search(r"no such table", msg, re.I) and re.match(r"\s*DROP\s+TABLE\b", sql, re.I):
        return OperationalError(1051, f"Unknown table ({msg})")
    if e.status == 0:
        return OperationalError(2013, f"Lost connection to MySQL server: {msg}")
    if e.status == 401:
        return OperationalError(1045, "Access denied for user (bad credentials)")
    if e.status == 404 and "database" in msg:
        return OperationalError(1049, f"Unknown database ({msg})")
    if e.status == 409:
        return OperationalError(1213, f"Write conflict; try restarting transaction ({msg})")
    if e.status == 403:
        return OperationalError(1290, msg)
    for pattern, cls, code, text in _SQLITE_TO_MYSQL:
        if re.search(pattern, msg, re.I):
            return cls(code, f"{text} ({msg})")
    return OperationalError(1105, msg)


# ---------------- placeholders: %s / %(name)s -> ?, dict params -> list ----------------

def _bind(sql, params):
    """Return (sql with ? placeholders, list of params). Quoted text is left alone."""
    names, out, i, n, quote = [], [], 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"', "`"):
            quote = ch
            out.append(ch)
        elif ch == "%" and i + 1 < n:
            nxt = sql[i + 1]
            if nxt == "s":
                out.append("?"); names.append(None); i += 1
            elif nxt == "%":
                out.append("%"); i += 1
            elif nxt == "(":
                end = sql.find(")s", i)
                if end == -1:
                    raise ProgrammingError(1064, f"bad placeholder near {sql[i:i+20]!r}")
                out.append("?"); names.append(sql[i + 2:end]); i = end + 1
            else:
                out.append(ch)
        else:
            out.append(ch)
        i += 1
    if isinstance(params, dict):
        if any(k is None for k in names):
            raise ProgrammingError(1064, "dict params need %(name)s placeholders")
        return "".join(out), [params[k] for k in names]
    return "".join(out), list(params or [])


# ---------------- unsupported features: fail loudly ----------------

_UNSUPPORTED = [
    (r"^\s*CREATE\s+(OR\s+REPLACE\s+)?(DEFINER\s*=\s*\S+\s+)?(PROCEDURE|FUNCTION|TRIGGER|EVENT)\b", "stored procedures / functions / triggers / events"),
    (r"^\s*(DROP|ALTER)\s+(PROCEDURE|FUNCTION|TRIGGER|EVENT)\b", "stored procedures / functions / triggers / events"),
    (r"^\s*(CALL|LOCK\s+TABLES|UNLOCK\s+TABLES|GRANT|REVOKE|FLUSH)\b", "CALL / LOCK TABLES / GRANT / REVOKE / FLUSH"),
    (r"^\s*(CREATE|ALTER|DROP|RENAME)\s+USER\b", "user management"),
    (r"^\s*SET\s+@[A-Za-z_]", "user variables (@x)"),
    (r"^\s*(CREATE|ALTER)\s+TABLE\b.*\bPARTITION\s+BY\b", "table partitioning"),
    (r"\bFOR\s+UPDATE\s*;?\s*$|\bLOCK\s+IN\s+SHARE\s+MODE\s*;?\s*$", "SELECT ... FOR UPDATE / LOCK IN SHARE MODE"),
    (r"\bFULLTEXT\b", "FULLTEXT indexes"),
]


def _check_supported(sql):
    for pattern, what in _UNSUPPORTED:
        if re.search(pattern, sql, re.I | re.S):
            raise NotSupportedError(1235, f"{what} need a real MySQL - migrate to RDS when you do")


# ---------------- metadata commands: answered from sqlite_master / PRAGMA, shaped like MySQL ----------------

_META = re.compile(
    r"^\s*(?:"
    r"(?P<tables>SHOW\s+(?:FULL\s+)?TABLES)"
    r"|(?P<dbs>SHOW\s+DATABASES)"
    r"|(?:SHOW\s+(?:FULL\s+)?COLUMNS\s+FROM|DESCRIBE|DESC)\s+`?(?P<cols>\w+)`?"
    r"|SHOW\s+CREATE\s+TABLE\s+`?(?P<create>\w+)`?"
    r"|SHOW\s+INDEX(?:ES)?\s+FROM\s+`?(?P<index>\w+)`?"
    r"|(?P<version>SELECT\s+VERSION\s*\(\s*\))"
    r"|(?P<database>SELECT\s+DATABASE\s*\(\s*\))"
    r")\s*;?\s*$", re.I)


def _result(columns, rows):
    return {"columns": columns, "rows": rows, "rowcount": -1, "lastrowid": None}


# ---------------- dialect translation (sqlglot) + fixes sqlglot does not do ----------------

# sqlglot may leave "ON DUPLICATE KEY UPDATE" or render "ON DUPLICATE KEY DO UPDATE SET"
_ON_DUP = re.compile(r"\bON\s+DUPLICATE\s+KEY(?:\s+DO)?\s+UPDATE(?:\s+SET)?\b", re.I)
_UPSERT_SPLIT = re.compile(r"\bON\s+CONFLICT\s+DO\s+UPDATE\s+SET\b", re.I)
_TRUNCATE = re.compile(r"^\s*TRUNCATE\s+(?:TABLE\s+)?`?(\w+)`?\s*$", re.I)
_VALUES_REF = re.compile(r"\bVALUES\s*\(\s*(\"?[A-Za-z_]\w*\"?)\s*\)", re.I)  # VALUES(col), never VALUES(1)
# MySQL index names are per table, SQLite's are per database: an index is stored as table__name and the
# prefix is added / stripped wherever the user sees a name (CREATE/DROP INDEX, SHOW INDEX, inline KEY)
_CREATE_INDEX = re.compile(r"^\s*CREATE\s+(UNIQUE\s+)?INDEX\s+`?(\w+)`?\s+ON\s+`?(\w+)`?", re.I)
_DROP_INDEX_ON = re.compile(r"^\s*DROP\s+INDEX\s+`?(\w+)`?\s+ON\s+`?(\w+)`?", re.I)
_RENAME_TABLE = re.compile(r"^\s*RENAME\s+TABLE\s+(.+)$", re.I | re.S)
_RENAME_PAIR = re.compile(r"`?(\w+)`?\s+TO\s+`?(\w+)`?", re.I)
_SESSION_NOOP = re.compile(r"^\s*SET\s+(?!@)", re.I)       # SET NAMES / SESSION / time_zone / sql_mode ...
_USE = re.compile(r"^\s*USE\s+`?(\w+)`?\s*$", re.I)
# LAST_INSERT_ID() / DATABASE(): per-session values the server does not know; the driver fills them in
_LAST_ID_TOKEN = "__S3RDS_LAST_INSERT_ID__"
_DATABASE_TOKEN = "__S3RDS_DATABASE__"

# MySQL functions sqlglot does not map for SQLite. ONE table, one line per function:
# key = sqlglot's name for the node (node.sql_name(), or node.name for unknown functions);
# value = SQLite SQL template with {0}, {1}, ... = the arguments ({0} defaults to 'now'), or a callable(node, args).
# A function missing here comes back as error 1305 "FUNCTION does not exist" -- add a line, nothing else.
_UUID = ("LOWER(HEX(RANDOMBLOB(4))) || '-' || LOWER(HEX(RANDOMBLOB(2))) || '-4' || SUBSTR(LOWER(HEX(RANDOMBLOB(2))), 2) "
         "|| '-' || SUBSTR('89ab', ABS(RANDOM()) % 4 + 1, 1) || SUBSTR(LOWER(HEX(RANDOMBLOB(2))), 2) || '-' || LOWER(HEX(RANDOMBLOB(6)))")
_PAD = ("CASE WHEN LENGTH({0}) >= {1} THEN SUBSTR({0}, 1, {1}) "
        "ELSE {left}SUBSTR(REPLACE(HEX(ZEROBLOB({1})), '00', {2}), 1, {1} - LENGTH({0})){right} END")
_SUBSTRING_INDEX = {   # only the common n = 1 / n = -1 forms (before the first / after the last delimiter)
    "1": "CASE WHEN INSTR({0}, {1}) > 0 THEN SUBSTR({0}, 1, INSTR({0}, {1}) - 1) ELSE {0} END",
    "-1": "REPLACE({0}, RTRIM({0}, REPLACE({0}, {1}, '')), '')",
}
_DIFF = {"SECOND": "* 86400", "MINUTE": "* 1440", "HOUR": "* 24", "DAY": "", "WEEK": "/ 7"}


def _date_add(node, args, sign):
    amount = args[1].strip("'")
    unit = node.args["unit"].name.lower() if node.args.get("unit") else "day"
    modifier = f"'{sign}{amount} {unit}'"
    if unit in ("hour", "minute", "second"):
        return f"DATETIME({args[0]}, {modifier})"
    # a DATE stays a DATE, a DATETIME stays a DATETIME (MySQL keeps the argument's type)
    return f"CASE WHEN LENGTH({args[0]}) <= 10 THEN DATE({args[0]}, {modifier}) ELSE DATETIME({args[0]}, {modifier}) END"


def _timestamp_diff(node, args):
    unit = node.args["unit"].name.upper()
    end, start = args[0], args[1]
    if unit in ("MONTH", "YEAR", "QUARTER"):
        months = (f"((CAST(STRFTIME('%Y', {end}) AS INTEGER) - CAST(STRFTIME('%Y', {start}) AS INTEGER)) * 12 "
                  f"+ CAST(STRFTIME('%m', {end}) AS INTEGER) - CAST(STRFTIME('%m', {start}) AS INTEGER))")
        return {"MONTH": months, "YEAR": f"({months} / 12)", "QUARTER": f"({months} / 3)"}[unit]
    if unit not in _DIFF:
        raise NotSupportedError(1582, f"TIMESTAMPDIFF unit {unit}")
    return f"CAST((JULIANDAY({end}) - JULIANDAY({start})) {_DIFF[unit]} AS INTEGER)"


def _substring_index(node, args):
    template = _SUBSTRING_INDEX.get(args[2])
    if not template:
        raise NotSupportedError(1582, "SUBSTRING_INDEX supports count 1 and -1 only")
    return template.format(*args)


def _concat_ws(node, args):
    """CONCAT_WS without SQLite's concat_ws() (3.44+; the Lambda runtime's SQLite is older): NULL arguments
    are skipped, a NULL separator gives NULL -- as MySQL."""
    sep, *items = [a.sql(dialect="sqlite") for a in node.expressions]
    if not items:
        raise NotSupportedError(1582, "wrong number of arguments for CONCAT_WS()")
    parts = " || ".join(f"COALESCE({sep} || ({x}), '')" for x in items)
    return f"SUBSTR({parts}, LENGTH({sep}) + 1)"


_FUNCS = {
    "NOW": "CURRENT_TIMESTAMP",
    "CURDATE": "CURRENT_DATE",
    "CURTIME": "CURRENT_TIME",
    "UNIX_TIMESTAMP": "CAST(strftime('%s', {0}) AS INTEGER)",
    "FROM_UNIXTIME": "datetime({0}, 'unixepoch')",
    "UNIX_TO_TIME": "datetime({0}, 'unixepoch')",   # sqlglot's own node name for FROM_UNIXTIME
    "LAST_INSERT_ID": _LAST_ID_TOKEN,
    "CURRENT_SCHEMA": f"'{_DATABASE_TOKEN}'",        # DATABASE()
    "CURRENT_VERSION": "'8.0.0-s3rds'",              # VERSION()
    "TRUNC": "(CAST(({0}) * POWER(10, {1}) AS INTEGER) / POWER(10, {1}))",   # TRUNCATE(x, d); sqlglot drops d
    "LEFT": "SUBSTR({0}, 1, {1})",
    "RIGHT": "SUBSTR({0}, MAX(LENGTH({0}) - ({1}), 0) + 1)",   # RIGHT(s, 0) = '', RIGHT(s, n > len) = s
    "REPEAT": "REPLACE(HEX(ZEROBLOB({1})), '00', {0})",
    "PAD": lambda node, args: _PAD.format(*args, left="" if node.args.get("is_left") else f"{args[0]} || ",
                                          right=f" || {args[0]}" if node.args.get("is_left") else ""),
    "SUBSTRING_INDEX": _substring_index,
    "CONCAT_WS": _concat_ws,
    "YEAR": "CAST(STRFTIME('%Y', {0}) AS INTEGER)",
    "MONTH": "CAST(STRFTIME('%m', {0}) AS INTEGER)",
    "DAY": "CAST(STRFTIME('%d', {0}) AS INTEGER)",
    "DAY_OF_MONTH": "CAST(STRFTIME('%d', {0}) AS INTEGER)",
    "HOUR": "CAST(STRFTIME('%H', {0}) AS INTEGER)",
    "MINUTE": "CAST(STRFTIME('%M', {0}) AS INTEGER)",
    "SECOND": "CAST(STRFTIME('%S', {0}) AS INTEGER)",
    "DAY_OF_WEEK": "(CAST(STRFTIME('%w', {0}) AS INTEGER) + 1)",      # MySQL: 1 = Sunday
    "DAY_OF_YEAR": "CAST(STRFTIME('%j', {0}) AS INTEGER)",
    "WEEK_OF_YEAR": "CAST(STRFTIME('%W', {0}) AS INTEGER)",
    "DATE_ADD": lambda node, args: _date_add(node, args, "+"),
    "DATE_SUB": lambda node, args: _date_add(node, args, "-"),
    "TIMESTAMPDIFF": _timestamp_diff,
    "JSON_EXTRACT": "({0} -> {1})",      # JSON_EXTRACT / ->: JSON text, as MySQL returns it ('1', '"x"', '{"a":1}')
    "JSON_EXTRACT_SCALAR": "({0} ->> {1})",   # ->>: the SQL value (a number comes back as int, MySQL gives '1')
    "JSON_UNQUOTE": "CASE WHEN JSON_VALID({0}) THEN JSON_EXTRACT({0}, '$') ELSE {0} END",
    "RAND": "(ABS(RANDOM()) / 9223372036854775808.0)",
    "UUID": _UUID,
}

# information_schema views, answered from sqlite_master / PRAGMA table-valued functions
_INFORMATION_SCHEMA = {
    "tables": (f"SELECT name AS table_name, '{_DATABASE_TOKEN}' AS table_schema, 'BASE TABLE' AS table_type, "
               "'InnoDB' AS engine FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_s3rds_%'"),
    "columns": (f"SELECT m.name AS table_name, '{_DATABASE_TOKEN}' AS table_schema, p.name AS column_name, "
                "p.cid + 1 AS ordinal_position, LOWER(p.type) AS column_type, LOWER(p.type) AS data_type, "
                "CASE WHEN p.\"notnull\" THEN 'NO' ELSE 'YES' END AS is_nullable, p.dflt_value AS column_default, "
                "CASE WHEN p.pk THEN 'PRI' ELSE '' END AS column_key "
                "FROM sqlite_master m JOIN pragma_table_info(m.name) p WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' AND m.name NOT LIKE '_s3rds_%'"),
}


def _rewrite_funcs(node):
    if isinstance(node, exp.Table) and node.db and node.db.lower() == "information_schema":
        view = _INFORMATION_SCHEMA.get(node.name.lower())
        if view is None:
            raise NotSupportedError(1235, f"information_schema.{node.name} is not available")
        return exp.Subquery(this=sqlglot.parse_one(view, read="sqlite"), alias=exp.to_identifier(node.alias or node.name))
    if not isinstance(node, exp.Func):
        return node
    # unknown functions are exp.Anonymous whose real name is node.name; known ones answer to sql_name()
    name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).upper()
    template = _FUNCS.get(name)
    if template is None:
        return node
    args = node.expressions if isinstance(node, exp.Anonymous) else \
        [v for v in node.args.values() if isinstance(v, exp.Expression)]
    arg_sql = [a.sql(dialect="sqlite") for a in args] or ["'now'"]
    try:
        text = template(node, arg_sql) if callable(template) else template.format(*arg_sql)
    except IndexError:
        raise NotSupportedError(1582, f"wrong number of arguments for {name}()") from None
    return exp.maybe_parse(text, dialect="sqlite")


_UNSIGNED = {"UINT": "INT", "UBIGINT": "BIGINT", "USMALLINT": "SMALLINT", "UTINYINT": "TINYINT",
             "UMEDIUMINT": "MEDIUMINT", "UDECIMAL": "DECIMAL", "UDOUBLE": "DOUBLE"}
_INT_TYPES = {"INT", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT", "BOOLEAN"}
_REAL_TYPES = {"DECIMAL", "FLOAT", "DOUBLE"}
# MySQL column types are kept in this table so DESCRIBE / SHOW CREATE TABLE show what the user wrote
META = "_s3rds_columns"
_META_DDL = f'CREATE TABLE IF NOT EXISTS "{META}" (tbl TEXT, col TEXT, type TEXT, extra TEXT, PRIMARY KEY (tbl, col))'
_HIDDEN = f"name NOT LIKE '{META}'"


def _q(text):
    return "'" + str(text).replace("'", "''") + "'"


def _mysql_type(kind):
    text = kind.sql(dialect="mysql").lower().replace(", ", ",")
    return {"boolean": "tinyint(1)", "timestamptz": "timestamp"}.get(text, text)


def _type_check(col, kind):
    """MySQL strict mode rejects 'abc' in an INT column; SQLite stores it. A named CHECK reproduces the error (1366)."""
    name = kind.this.name
    if name == "USERDEFINED":
        name = str(kind.args.get("kind") or "").split("(")[0].upper()
    allowed = "'integer', 'null'" if name in _INT_TYPES else "'integer', 'real', 'null'" if name in _REAL_TYPES else None
    if allowed is None:
        return None
    return exp.Constraint(this=exp.to_identifier(f"s3rds_type_{col}"),
                          expressions=[exp.CheckColumnConstraint(this=exp.condition(f'TYPEOF("{col}") IN ({allowed})'))])
_DROPPED_CONSTRAINTS = (exp.OnUpdateColumnConstraint, exp.CharacterSetColumnConstraint,
                        exp.CommentColumnConstraint, exp.CollateColumnConstraint)


def _index_sql(table, name, cols, unique):
    name = name or cols[0]   # MySQL names an unnamed index after its first column
    return f'CREATE {"UNIQUE " if unique else ""}INDEX "{table}__{name}" ON "{table}" ({", ".join(cols)})'


def _fix_create_table(tree):
    """MySQL CREATE TABLE -> something SQLite accepts with MySQL-like semantics.
    Returns the CREATE INDEX statements for inline KEY / INDEX / named UNIQUE KEY definitions."""
    if not (isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() == "TABLE"):
        return []
    schema = tree.this
    if not isinstance(schema, exp.Schema):
        return []
    tree.set("properties", None)  # ENGINE=, CHARSET=, COMMENT=, ...
    table = schema.this.name
    meta = [_META_DDL, f'DELETE FROM "{META}" WHERE tbl = {_q(table)}']
    for item in schema.expressions:
        if isinstance(item, exp.ColumnDef) and item.args.get("kind") is not None:
            auto = any(isinstance(c.kind, exp.AutoIncrementColumnConstraint) for c in item.args.get("constraints") or [])
            meta.append(f'INSERT INTO "{META}" VALUES ({_q(table)}, {_q(item.name)}, {_q(_mysql_type(item.args["kind"]))}, '
                        f'{_q("auto_increment" if auto else "")})')

    kept, indexes, checks, table_pk_cols = [], [], [], set()
    for item in schema.expressions:
        if isinstance(item, exp.PrimaryKey):
            cols = [c.name for c in item.expressions]
            if len(cols) == 1:          # single-column PK: moved into the column (needed for AUTOINCREMENT)
                table_pk_cols.add(cols[0])
                continue
        if isinstance(item, exp.IndexColumnConstraint):
            indexes.append(_index_sql(table, item.name, [c.name for c in item.expressions], False))
            continue
        if isinstance(item, exp.UniqueColumnConstraint) and isinstance(item.this, exp.Schema) and item.this.this:
            indexes.append(_index_sql(table, item.this.this.name, [c.name for c in item.this.expressions], True))
            continue
        if isinstance(item, (exp.ColumnDef, exp.PrimaryKey, exp.ForeignKey, exp.Constraint,
                             exp.CheckColumnConstraint, exp.UniqueColumnConstraint)):
            kept.append(item)

    for col in kept:
        if not isinstance(col, exp.ColumnDef):
            continue
        kind = col.args.get("kind")
        if kind is not None and kind.this.name in _UNSIGNED:
            col.set("kind", exp.DataType.build(_UNSIGNED[kind.this.name]))
            kind = col.args["kind"]
        # keep DECIMAL / TIMESTAMP as the declared type (sqlglot would write REAL / TIMESTAMPTZ): the server
        # marks result values by the declared type, so the driver can hand back Decimal / datetime
        if kind is not None and kind.this == exp.DataType.Type.DECIMAL:
            col.set("kind", exp.DataType(this=exp.DataType.Type.USERDEFINED, kind=kind.sql(dialect="mysql")))
            kind = col.args["kind"]
        elif kind is not None and kind.this == exp.DataType.Type.TIMESTAMPTZ:
            col.set("kind", exp.DataType(this=exp.DataType.Type.USERDEFINED, kind="TIMESTAMP"))
            kind = col.args["kind"]
        all_constraints = col.args.get("constraints") or []
        binary = any(isinstance(c.kind, exp.CollateColumnConstraint) and c.kind.this.name.lower().endswith(("_bin", "_cs"))
                     for c in all_constraints)
        constraints = [c for c in all_constraints if not isinstance(c.kind, _DROPPED_CONSTRAINTS)]
        kinds = [type(c.kind) for c in constraints]
        is_pk = exp.PrimaryKeyColumnConstraint in kinds or col.name in table_pk_cols
        is_auto = exp.AutoIncrementColumnConstraint in kinds

        if kind is not None and kind.this == exp.DataType.Type.ENUM:
            values = [v.this for v in kind.expressions]
            col.set("kind", exp.DataType.build("TEXT"))
            constraints.append(exp.ColumnConstraint(kind=exp.CheckColumnConstraint(
                this=exp.In(this=exp.column(col.name), expressions=[exp.Literal.string(v) for v in values]))))
            kind = col.args["kind"]

        if is_auto:
            col.set("kind", exp.DataType.build("INTEGER"))
            is_pk = True
        if kind is not None and kind.this in exp.DataType.TEXT_TYPES and not binary:
            constraints.append(exp.ColumnConstraint(kind=exp.CollateColumnConstraint(this=exp.var("NOCASE"))))

        others = [c for c in constraints
                  if not isinstance(c.kind, (exp.PrimaryKeyColumnConstraint, exp.AutoIncrementColumnConstraint))]
        tail = []
        if is_pk:
            tail.append(exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint()))
        if is_auto:
            tail.append(exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint()))  # must follow PRIMARY KEY
        col.set("constraints", others + tail)
        if kind is not None and not is_auto:
            check = _type_check(col.name, kind)
            if check is not None:
                checks.append(check)

    schema.set("expressions", kept + checks)
    return indexes + meta


def _meta_stmts(tree):
    """Statements that keep the MySQL column-type table in step with a DDL statement."""
    if isinstance(tree, exp.Drop) and (tree.args.get("kind") or "").upper() == "TABLE":
        return [_META_DDL] + [f'DELETE FROM "{META}" WHERE tbl = {_q(t.name)}' for t in tree.args.get("tables") or []]
    if not isinstance(tree, exp.Alter):
        return []
    table = tree.this.name
    out = [_META_DDL]
    for action in tree.args.get("actions") or []:
        if isinstance(action, exp.ColumnDef) and action.args.get("kind") is not None:      # ADD COLUMN
            out.append(f'INSERT OR REPLACE INTO "{META}" VALUES ({_q(table)}, {_q(action.name)}, '
                       f'{_q(_mysql_type(action.args["kind"]))}, \'\')')
        elif isinstance(action, exp.Drop):                                                  # DROP COLUMN
            out += [f'DELETE FROM "{META}" WHERE tbl = {_q(table)} AND col = {_q(c.name)}' for c in action.args.get("tables") or []]
        elif isinstance(action, exp.RenameColumn):
            out.append(f'UPDATE "{META}" SET col = {_q(action.args["to"].name)} WHERE tbl = {_q(table)} AND col = {_q(action.this.name)}')
        elif isinstance(action, exp.AlterRename):
            out.append(f'UPDATE "{META}" SET tbl = {_q(action.this.name)} WHERE tbl = {_q(table)}')
        elif isinstance(action, exp.ModifyColumn) and action.this.args.get("kind") is not None:   # MODIFY / CHANGE
            old = action.args.get("rename_from")
            where = f'tbl = {_q(table)} AND col = {_q(old.name if old is not None else action.this.name)}'
            out.append(f'UPDATE "{META}" SET col = {_q(action.this.name)}, type = {_q(_mysql_type(action.this.args["kind"]))} WHERE {where}')
    return out


def _fix_alter(tree):
    """ALTER TABLE MODIFY / CHANGE: SQLite cannot change a column's type, so a CHANGE becomes a RENAME COLUMN
    and a type-only MODIFY is a no-op (SQLite is dynamically typed). Returns the statements to run."""
    if not isinstance(tree, exp.Alter):
        return None
    out = []
    for action in tree.args.get("actions") or []:
        if isinstance(action, exp.ModifyColumn):
            old = action.args.get("rename_from")
            if old is not None and old.name != action.this.name:
                out.append(f'ALTER TABLE "{tree.this.name}" RENAME COLUMN "{old.name}" TO "{action.this.name}"')
        else:
            return None   # anything else: let sqlglot render it
    return out + _meta_stmts(tree)


def _fix_insert(tree):
    """VALUES () -> DEFAULT VALUES; a DEFAULT keyword drops its column from the statement."""
    if not isinstance(tree, exp.Insert) or not isinstance(tree.expression, exp.Values):
        return
    rows = tree.expression.expressions
    if len(rows) == 1 and not rows[0].expressions:
        return f'INSERT INTO "{tree.this.find(exp.Table).name}" DEFAULT VALUES'
    schema = tree.this
    if not isinstance(schema, exp.Schema):
        return
    default_cols = {i for i, v in enumerate(rows[0].expressions) if isinstance(v, exp.Var) and v.name.upper() == "DEFAULT"}
    if not default_cols:
        return
    for row in rows:
        if {i for i, v in enumerate(row.expressions) if isinstance(v, exp.Var) and v.name.upper() == "DEFAULT"} != default_cols:
            raise NotSupportedError(1582, "DEFAULT must be used in the same columns of every row")
    schema.set("expressions", [c for i, c in enumerate(schema.expressions) if i not in default_cols])
    for row in rows:
        row.set("expressions", [v for i, v in enumerate(row.expressions) if i not in default_cols])


def _fix_update_join(tree):
    """UPDATE t JOIN o ON ... SET t.a = o.b [WHERE w]  ->  UPDATE t SET a = o.b FROM o WHERE ... AND w"""
    if not isinstance(tree, exp.Update) or not tree.this.args.get("joins"):
        return None
    target = tree.this
    joins = target.args["joins"]
    conds = [j.args["on"] for j in joins if j.args.get("on")]
    if tree.args.get("where"):
        conds.append(tree.args["where"].this)
    sets = []
    for e in tree.expressions:
        col = e.this
        if isinstance(col, exp.Column) and col.table == target.name:
            col.set("table", None)
        sets.append(e.sql(dialect="sqlite"))
    from_tables = ", ".join(j.this.sql(dialect="sqlite") for j in joins)
    where = " AND ".join(f"({c.sql(dialect='sqlite')})" for c in conds)
    return f'UPDATE "{target.name}" SET {", ".join(sets)} FROM {from_tables} WHERE {where}'


def _fix_delete_join(tree):
    """DELETE t FROM t JOIN o ON ... [WHERE w]  ->  DELETE FROM t WHERE EXISTS (SELECT 1 FROM o WHERE ... AND w)"""
    if not isinstance(tree, exp.Delete) or not tree.this.args.get("joins"):
        return None
    target = tree.this
    joins = target.args["joins"]
    conds = [j.args["on"] for j in joins if j.args.get("on")]
    if tree.args.get("where"):
        conds.append(tree.args["where"].this)
    from_tables = ", ".join(j.this.sql(dialect="sqlite") for j in joins)
    where = " AND ".join(f"({c.sql(dialect='sqlite')})" for c in conds)
    return f'DELETE FROM "{target.name}" WHERE EXISTS (SELECT 1 FROM {from_tables} WHERE {where})'


@functools.lru_cache(maxsize=1024)
def _translate_stmts(sql):
    """MySQL statement -> tuple of SQLite statements (usually one; CREATE TABLE with inline indexes and
    RENAME TABLE give several, a type-only ALTER gives none)."""
    if sqlglot is None:
        raise NotSupportedError(0, "the MySQL dialect needs sqlglot (an s3rds dependency): pip install s3rds")
    m = _RENAME_TABLE.match(sql)
    if m:
        pairs = _RENAME_PAIR.findall(m.group(1))
        return tuple([f'ALTER TABLE "{a}" RENAME TO "{b}"' for a, b in pairs] + [_META_DDL]
                     + [f'UPDATE "{META}" SET tbl = {_q(b)} WHERE tbl = {_q(a)}' for a, b in pairs])
    m = _CREATE_INDEX.match(sql)
    if m:
        sql = _CREATE_INDEX.sub(lambda m: f'CREATE {m.group(1) or ""}INDEX "{m.group(3)}__{m.group(2)}" ON "{m.group(3)}"', sql, 1)
    m = _DROP_INDEX_ON.match(sql)
    if m:
        return (f'DROP INDEX "{m.group(2)}__{m.group(1)}"',)
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except ParseError as e:
        raise ProgrammingError(1064, f"You have an error in your SQL syntax ({e})") from None
    for fix in (_fix_alter, _fix_update_join, _fix_delete_join, _fix_insert):
        rewritten = fix(tree)
        if rewritten is not None:
            return tuple(rewritten) if isinstance(rewritten, list) else (rewritten,)
    extra = _fix_create_table(tree) + _meta_stmts(tree)
    tree = tree.transform(_rewrite_funcs)
    out = tree.sql(dialect="sqlite")
    out = _ON_DUP.sub("ON CONFLICT DO UPDATE SET", out)   # upsert
    parts = _UPSERT_SPLIT.split(out, maxsplit=1)
    if len(parts) == 2:  # VALUES(col) -> excluded.col, only inside the DO UPDATE SET clause
        out = parts[0] + "ON CONFLICT DO UPDATE SET" + _VALUES_REF.sub(r"excluded.\1", parts[1])
    return (out, *extra)


# ---------------- result types MySQL reports but SQLite cannot know ----------------
# Column types come from the server (declared type markers). For expressions the SQL itself decides:
# SUM / AVG over an integer or DECIMAL column and integer division are Decimal, NOW() is a datetime.

def _expr_tag(node):
    if isinstance(node, (exp.Sum, exp.Avg)):
        col = node.this
        return ("agg", col.name if isinstance(col, exp.Column) else None)
    if isinstance(node, exp.Div):
        cols = [x.name for x in (node.this, node.expression) if isinstance(x, exp.Column)]
        lits = [x for x in (node.this, node.expression) if isinstance(x, exp.Literal)]
        if any(not x.is_string and ("." in x.name or "e" in x.name.lower()) for x in lits):
            return None   # a float literal makes the whole division a float, as in MySQL
        return ("div", cols)
    if isinstance(node, exp.CurrentTimestamp) or isinstance(node, exp.Anonymous) and node.name.upper() in ("NOW", "SYSDATE"):
        return ("datetime", None)
    if isinstance(node, exp.CurrentDate) or isinstance(node, exp.Anonymous) and node.name.upper() == "CURDATE":
        return ("date", None)
    if isinstance(node, exp.CurrentTime) or isinstance(node, exp.Anonymous) and node.name.upper() == "CURTIME":
        return ("time", None)
    return None


@functools.lru_cache(maxsize=1024)
def _result_tags(sql):
    """(tags per select column, tables used) or None when the statement is not a plain SELECT."""
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except ParseError:
        return None
    if not isinstance(tree, exp.Select):
        return None
    tags = tuple(_expr_tag(e.this if isinstance(e, exp.Alias) else e) for e in tree.expressions)
    if not any(tags):
        return None
    return tags, tuple(t.name for t in tree.find_all(exp.Table))


def _to_timedelta(t):
    return datetime.timedelta(hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond)


def _translate(sql):
    """One MySQL statement -> SQLite SQL (statements joined with '; ' when there are several)."""
    return "; ".join(_translate_stmts(sql))


# ---------------- connection ----------------

_INSERT_TARGET = re.compile(r"^\s*(?:INSERT|REPLACE)\b(?:\s+OR\s+\w+)?(?:\s+IGNORE)?\s+INTO\s+[`\"]?(\w+)", re.I)


class Connection(_sqlite.Connection):
    _last_insert_id = 0  # MySQL's per-session LAST_INSERT_ID()
    _typed = True        # the server marks DATETIME / DATE / TIME / DECIMAL columns

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._autoinc = {}   # table -> has AUTOINCREMENT (for MySQL-style lastrowid)
        self._coltypes = {}  # table -> {column: MySQL type} (for aggregate result types)

    def _types_of(self, table):
        if table not in self._coltypes:
            try:
                rows = self._raw(f'SELECT col, type FROM "{META}" WHERE tbl = ?', [table])["rows"]
            except ProgrammingError:
                rows = []
            self._coltypes[table] = {c: t for c, t in rows}
        return self._coltypes[table]

    def _column_type(self, tables, col):
        for t in tables:
            ty = self._types_of(t).get(col)
            if ty:
                return ty
        return ""

    def _type_results(self, result, sql):
        """Apply MySQL result types: markers from the server are already decoded by the base driver;
        TIME is a timedelta in pymysql; SUM / AVG / division become Decimal by the SQL's shape."""
        rows = result.get("rows") or []
        if not rows:
            return
        if any(isinstance(v, datetime.time) for v in rows[0]):
            result["rows"] = rows = [[_to_timedelta(v) if isinstance(v, datetime.time) else v for v in r] for r in rows]
        found = _result_tags(sql)
        if not found or len(found[0]) != len(result.get("columns") or []):
            return
        tags, tables = found
        conv = {}
        for i, tag in enumerate(tags):
            if not tag:
                continue
            kind, arg = tag
            if kind == "agg":
                ty = self._column_type(tables, arg) if arg else ""
                if ty.split("(")[0] in ("float", "double", "real"):
                    continue
                conv[i] = lambda v: decimal.Decimal(str(v)) if isinstance(v, (int, float)) else v
            elif kind == "div":
                if any(self._column_type(tables, c).split("(")[0] in ("float", "double", "real") for c in arg):
                    continue
                conv[i] = lambda v: decimal.Decimal(str(v)) if isinstance(v, (int, float)) else v
            elif kind == "datetime":
                conv[i] = lambda v: datetime.datetime.fromisoformat(v) if isinstance(v, str) else v
            elif kind == "date":
                conv[i] = lambda v: datetime.date.fromisoformat(v) if isinstance(v, str) else v
            elif kind == "time":
                conv[i] = lambda v: _to_timedelta(datetime.time.fromisoformat(v)) if isinstance(v, str) else v
        if conv:
            result["rows"] = [[conv[i](v) if i in conv and v is not None else v for i, v in enumerate(r)] for r in rows]

    def _prepare(self, sql):
        sql = sql.strip().rstrip(";")
        _check_supported(sql)
        return sql

    def _finalize(self, translated):
        return translated.replace(_LAST_ID_TOKEN, str(self._last_insert_id)).replace(_DATABASE_TOKEN, self.database)

    def _noop(self, sql):
        return self._remember({"columns": [], "rows": [], "rowcount": 0, "lastrowid": None}, sql)

    def _has_autoincrement(self, sql):
        """MySQL reports lastrowid 0 unless the table has AUTO_INCREMENT; SQLite always has a rowid."""
        m = _INSERT_TARGET.match(sql)
        if not m:
            return False
        table = m.group(1)
        if table not in self._autoinc:
            r = self._raw("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", [table])
            self._autoinc[table] = bool(r["rows"]) and "AUTOINCREMENT" in r["rows"][0][0].upper()
        return self._autoinc[table]

    def _remember(self, result, sql=""):
        if not sql.lstrip()[:6].upper() in ("INSERT", "UPDATE", "DELETE", "SELECT", "REPLAC"):
            self._autoinc.clear()   # DDL may have changed a table
            self._coltypes.clear()
        if result.get("columns"):
            self._type_results(result, sql)
        if result.get("lastrowid") and not result.get("columns") and not self._has_autoincrement(sql):
            result["lastrowid"] = 0
        # multi-row INSERT: MySQL's lastrowid is the FIRST generated id, SQLite's the last
        if result.get("lastrowid") and result.get("rowcount", 0) > 1 and not result.get("columns"):
            result["lastrowid"] = result["lastrowid"] - result["rowcount"] + 1
        if result.get("lastrowid"):
            self._last_insert_id = result["lastrowid"]
        # pymysql's rowcount: number of rows for SELECT, 0 (not SQLite's -1) for DDL
        if result.get("columns"):
            result["rowcount"] = len(result.get("rows") or [])
        elif result.get("rowcount", -1) < 0:
            result["rowcount"] = 0
        return result

    def execute(self, sql, params=()):
        sql = self._prepare(sql)
        m = _META.match(sql)
        if m:
            return self._meta(m)
        m = _TRUNCATE.match(sql)
        if m:  # MySQL TRUNCATE = delete every row AND reset AUTO_INCREMENT; one transaction, one S3 write
            table = m.group(1)
            _sqlite.Connection.transaction(self, [
                (f'DELETE FROM "{table}"', []),
                ("DELETE FROM sqlite_sequence WHERE name = ?", [table]),
            ])
            return self._remember({"columns": [], "rows": [], "rowcount": 0, "lastrowid": None,
                                   "ms": self.last.get("ms"), "s3": self.last.get("s3")}, sql)
        m = _USE.match(sql)
        if m:
            self.database = m.group(1)
            return self._noop(sql)
        if _SESSION_NOOP.match(sql):   # SET NAMES / SESSION / sql_mode / time_zone: nothing to configure here
            return self._noop(sql)
        qmark, args = _bind(sql, params)
        stmts = _translate_stmts(qmark)
        try:
            if len(stmts) == 1:
                return self._remember(super().execute(self._finalize(stmts[0]), args), sql)
            if not stmts:
                return self._noop(sql)
            results = _sqlite.Connection.transaction(self, [(self._finalize(stmts[0]), args)] + [(self._finalize(x), []) for x in stmts[1:]])
            return self._remember(results[0], sql)
        except _sqlite.Error as e:
            raise _map_error(e, sql) from None

    def transaction(self, statements):
        prepared = []
        for s in statements:
            sql, params = (s, []) if isinstance(s, str) else (s[0], s[1] if len(s) > 1 else [])
            qmark, args = _bind(self._prepare(sql), params)
            stmts = _translate_stmts(qmark)
            prepared += [(self._finalize(stmts[0]), args)] + [(self._finalize(x), []) for x in stmts[1:]]
        try:
            results = super().transaction(prepared)
        except _sqlite.Error as e:
            raise _map_error(e) from None
        for r, (sql, _) in zip(results, prepared):
            self._remember(r, sql)
        return results[:len(statements)] if len(results) > len(statements) else results

    def create_database(self):
        try:
            super().create_database()
        except _sqlite.Error as e:
            raise _map_error(e) from None

    def _raw(self, sql, params=()):
        """SQLite SQL straight to the endpoint (for metadata queries)."""
        try:
            return _sqlite.Connection.execute(self, sql, params)
        except _sqlite.Error as e:
            raise _map_error(e) from None

    def _indexes(self, table):
        """[(name, unique, [columns])] excluding the primary key; SQLite's auto index for an inline
        UNIQUE is named after its column, as MySQL does."""
        out = []
        for _seq, name, unique, origin, _partial in self._raw(f'PRAGMA index_list("{table}")')["rows"]:
            if origin == "pk":
                continue
            cols = [c for _s, _cid, c in self._raw(f'PRAGMA index_info("{name}")')["rows"]]
            shown = cols[0] if name.startswith("sqlite_autoindex_") else name.split("__", 1)[1] if name.startswith(table + "__") else name
            out.append((shown, bool(unique), cols))
        return out

    def _describe(self, table, info):
        """DESCRIBE rows from PRAGMA table_info + the MySQL types we stored at CREATE TABLE."""
        types = self._types_of(table)
        extras = {}
        try:
            extras = {c: e for c, e in self._raw(f'SELECT col, extra FROM "{META}" WHERE tbl = ?', [table])["rows"]}
        except ProgrammingError:
            pass
        keys = {}   # column -> UNI / MUL from the first index that starts with it, like MySQL
        for _n, _u, cols in self._indexes(table):
            keys.setdefault(cols[0], "UNI" if _u else "MUL")
        rows = []
        for _cid, name, ctype, notnull, default, pk in info:
            auto = extras.get(name) or ("auto_increment" if pk and (ctype or "").upper() == "INTEGER" else "")
            if isinstance(default, str) and len(default) >= 2 and default[0] == default[-1] == "'":
                default = default[1:-1].replace("''", "'")
            rows.append([name, types.get(name) or (ctype or "").lower(), "NO" if notnull or pk else "YES",
                         "PRI" if pk else keys.get(name, ""), default, auto])
        return rows

    def _create_table(self, table, info):
        """SHOW CREATE TABLE in MySQL's layout, from the same metadata DESCRIBE uses."""
        lines = []
        pk = []
        for name, ctype, null, key, default, extra in self._describe(table, info):
            line = f"  `{name}` {ctype}"
            if null == "NO":
                line += " NOT NULL"
            if default is not None:
                line += f" DEFAULT {default}" if default.upper() == "CURRENT_TIMESTAMP" else f" DEFAULT '{default}'"
            elif null == "YES":
                line += " DEFAULT NULL"
            if extra:
                line += f" {extra.upper()}"
            lines.append(line)
            if key == "PRI":
                pk.append(name)
        if pk:
            lines.append("  PRIMARY KEY (" + ", ".join(f"`{c}`" for c in pk) + ")")
        for name, unique, cols in self._indexes(table):
            lines.append(f"  {'UNIQUE ' if unique else ''}KEY `{name}` (" + ", ".join(f"`{c}`" for c in cols) + ")")
        return f"CREATE TABLE `{table}` (\n" + ",\n".join(lines) + "\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"

    def _meta(self, m):
        if m.group("tables"):
            r = self._raw(f"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND {_HIDDEN} ORDER BY name")
            return _result([f"Tables_in_{self.database}"], r["rows"])
        if m.group("dbs"):
            try:
                names = self.list_databases()
            except _sqlite.Error as e:
                raise _map_error(e) from None
            return _result(["Database"], [[n] for n in names])
        if m.group("cols"):
            table = m.group("cols")
            info = self._raw(f'PRAGMA table_info("{table}")')["rows"]
            if not info:
                raise ProgrammingError(1146, f"Table '{table}' doesn't exist")
            return _result(["Field", "Type", "Null", "Key", "Default", "Extra"], self._describe(table, info))
        if m.group("create"):
            table = m.group("create")
            info = self._raw(f'PRAGMA table_info("{table}")')["rows"]
            if not info:
                raise ProgrammingError(1146, f"Table '{table}' doesn't exist")
            return _result(["Table", "Create Table"], [[table, self._create_table(table, info)]])
        if m.group("index"):
            table = m.group("index")
            info = self._raw(f'PRAGMA table_info("{table}")')["rows"]
            if not info:
                raise ProgrammingError(1146, f"Table '{table}' doesn't exist")
            pk = [(r[1], r[5]) for r in info if r[5]]
            rows = [[table, 0, "PRIMARY", seq, name] for name, seq in sorted(pk, key=lambda x: x[1])]
            for name, unique, cols in self._indexes(table):
                rows += [[table, 0 if unique else 1, name, i + 1, c] for i, c in enumerate(cols)]
            return _result(["Table", "Non_unique", "Key_name", "Seq_in_index", "Column_name"], rows)
        if m.group("version"):
            return _result(["VERSION()"], [[f"8.0.0-s3rds-{__version__}"]])
        if m.group("database"):
            return _result(["DATABASE()"], [[self.database]])
        raise InternalError(0, "unreachable")


def connect(host, user="", password="", database="", timeout=30,
            cursorclass=Cursor, **_ignored):
    """Same signature as pymysql.connect; extra pymysql arguments are accepted and ignored."""
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, database, user, password, timeout, cursorclass)
