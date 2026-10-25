"""pys3mysql — the MySQL-dialect driver for pys3rds. Write MySQL; it runs on the pys3rds endpoint (SQLite engine).

    pip install "pys3rds[mysql]"          # pulls in sqlglot for the dialect translation

    import pys3mysql                       # later: import pymysql on a real RDS, nothing else changes
    conn = pys3mysql.connect(host="xxx.lambda-url.us-east-2.on.aws",
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
  - INSERT IGNORE, ON DUPLICATE KEY UPDATE (VALUES(col) -> excluded.col)
  - table options (ENGINE=, CHARSET=), ON UPDATE CURRENT_TIMESTAMP and inline KEY/INDEX are dropped
  - functions sqlglot knows (NOW, IFNULL, CONCAT_WS, DATE_FORMAT, ...), LAST_INSERT_ID()
  - SHOW TABLES / DATABASES / COLUMNS / CREATE TABLE / INDEX, DESCRIBE, SELECT VERSION(), SELECT DATABASE()
  - errors come back as pymysql-style classes with MySQL codes: 1062, 1146, 1054, 1064, 1048, 1452, ...

Not supported, raised as NotSupportedError (never silently wrong): stored procedures/functions,
triggers, FULLTEXT, SELECT ... FOR UPDATE, LOCK TABLES, GRANT/REVOKE/users.
Values are returned as stored (no DATETIME -> datetime.datetime conversion in this version).
"""
import functools
import re

import pys3sqlite
from pys3sqlite import Cursor, DictCursor, cursors  # noqa: F401  (same DB-API cursors)

try:
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError
except ImportError:  # the error surfaces on first query, with install instructions
    sqlglot = None

__version__ = pys3sqlite.__version__
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
    (r"CHECK constraint failed",       IntegrityError,   3819, "Check constraint is violated"),
    (r"no such table",                 ProgrammingError, 1146, "Table doesn't exist"),
    (r"no such column",                ProgrammingError, 1054, "Unknown column"),
    (r"already exists",                OperationalError, 1050, "Table already exists"),
    (r"no such function",              ProgrammingError, 1305, "FUNCTION does not exist"),
    (r"syntax error|incomplete input", ProgrammingError, 1064, "You have an error in your SQL syntax"),
    (r"readonly|read-only",            OperationalError, 1290, "The server is running in read-only mode"),
    (r"cannot store .* value in .* column", DataError, 1366, "Incorrect value for column"),
]


def _map_error(e):
    """pys3sqlite.Error (HTTP status + server message) -> pymysql-style exception with a MySQL code."""
    msg = e.message or str(e)
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
        missing = [k for k in names if k is None]
        if missing:
            raise ProgrammingError(1064, "dict params need %(name)s placeholders")
        return "".join(out), [params[k] for k in names]
    return "".join(out), list(params or [])


# ---------------- unsupported features: fail loudly ----------------

_UNSUPPORTED = [
    (r"^\s*CREATE\s+(OR\s+REPLACE\s+)?(DEFINER\s*=\s*\S+\s+)?(PROCEDURE|FUNCTION|TRIGGER|EVENT)\b", "stored procedures / functions / triggers / events"),
    (r"^\s*(CALL|LOCK\s+TABLES|UNLOCK\s+TABLES|GRANT|REVOKE|FLUSH)\b", "CALL / LOCK TABLES / GRANT / REVOKE / FLUSH"),
    (r"^\s*(CREATE|ALTER|DROP|RENAME)\s+USER\b", "user management"),
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

_ON_DUP = re.compile(r"\bON\s+DUPLICATE\s+KEY\s+UPDATE\b", re.I)
_VALUES_REF = re.compile(r"\bVALUES\s*\(\s*(\"?[\w]+\"?)\s*\)", re.I)
_LAST_ID = re.compile(r"\bLAST_INSERT_ID\s*\(\s*\)", re.I)


def _fix_create_table(tree):
    """MySQL CREATE TABLE -> something SQLite accepts with MySQL-like semantics."""
    if not (isinstance(tree, exp.Create) and (tree.args.get("kind") or "").upper() == "TABLE"):
        return
    schema = tree.this
    if not isinstance(schema, exp.Schema):
        return
    tree.set("properties", None)  # ENGINE=, CHARSET=, COMMENT=, ...

    kept, table_pk_cols = [], set()
    for item in schema.expressions:
        if isinstance(item, exp.PrimaryKey):
            cols = [c.name for c in item.expressions]
            if len(cols) == 1:          # single-column PK: moved into the column (needed for AUTOINCREMENT)
                table_pk_cols.add(cols[0])
                continue
        if isinstance(item, (exp.ColumnDef, exp.PrimaryKey, exp.ForeignKey)) or "Unique" in type(item).__name__:
            kept.append(item)           # inline KEY/INDEX definitions are dropped (CREATE INDEX separately)

    for col in kept:
        if not isinstance(col, exp.ColumnDef):
            continue
        kind = col.args.get("kind")
        constraints = [c for c in (col.args.get("constraints") or [])
                       if not isinstance(c.kind, exp.OnUpdateColumnConstraint)]
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
        if kind is not None and kind.this in exp.DataType.TEXT_TYPES \
                and exp.CollateColumnConstraint not in kinds:
            constraints.append(exp.ColumnConstraint(kind=exp.CollateColumnConstraint(this=exp.var("NOCASE"))))

        others = [c for c in constraints
                  if not isinstance(c.kind, (exp.PrimaryKeyColumnConstraint, exp.AutoIncrementColumnConstraint))]
        tail = []
        if is_pk:
            tail.append(exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint()))
        if is_auto:
            tail.append(exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint()))  # must follow PRIMARY KEY
        col.set("constraints", others + tail)

    schema.set("expressions", kept)


@functools.lru_cache(maxsize=1024)
def _translate(sql):
    if sqlglot is None:
        raise NotSupportedError(0, "the MySQL dialect needs sqlglot: pip install 'pys3rds[mysql]'")
    try:
        tree = sqlglot.parse_one(sql, read="mysql")
    except ParseError as e:
        raise ProgrammingError(1064, f"You have an error in your SQL syntax ({e})") from None
    _fix_create_table(tree)
    out = tree.sql(dialect="sqlite")
    out = _ON_DUP.sub("ON CONFLICT DO UPDATE SET", out)   # upsert, if sqlglot left MySQL syntax
    out = _VALUES_REF.sub(r"excluded.\1", out)
    out = _LAST_ID.sub("last_insert_rowid()", out)
    return out


# ---------------- connection ----------------

class Connection(pys3sqlite.Connection):
    def execute(self, sql, params=()):
        sql = sql.strip().rstrip(";")
        _check_supported(sql)
        m = _META.match(sql)
        if m:
            return self._meta(m)
        qmark, args = _bind(sql, params)
        try:
            return super().execute(_translate(qmark), args)
        except pys3sqlite.Error as e:
            raise _map_error(e) from None

    def transaction(self, statements):
        prepared = []
        for s in statements:
            sql, params = (s, []) if isinstance(s, str) else (s[0], s[1] if len(s) > 1 else [])
            sql = sql.strip().rstrip(";")
            _check_supported(sql)
            qmark, args = _bind(sql, params)
            prepared.append((_translate(qmark), args))
        try:
            return super().transaction(prepared)
        except pys3sqlite.Error as e:
            raise _map_error(e) from None

    def create_database(self):
        try:
            super().create_database()
        except pys3sqlite.Error as e:
            raise _map_error(e) from None

    def _raw(self, sql, params=()):
        """SQLite SQL straight to the endpoint (for metadata queries)."""
        try:
            return pys3sqlite.Connection.execute(self, sql, params)
        except pys3sqlite.Error as e:
            raise _map_error(e) from None

    def _meta(self, m):
        if m.group("tables"):
            r = self._raw("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
            return _result([f"Tables_in_{self.database}"], r["rows"])
        if m.group("dbs"):
            try:
                names = self._list_databases()
            except pys3sqlite.Error as e:
                raise _map_error(e) from None
            return _result(["Database"], [[n] for n in names])
        if m.group("cols"):
            table = m.group("cols")
            info = self._raw(f'PRAGMA table_info("{table}")')["rows"]
            if not info:
                raise ProgrammingError(1146, f"Table '{table}' doesn't exist")
            rows = []
            for _cid, name, ctype, notnull, default, pk in info:
                auto = "auto_increment" if pk and (ctype or "").upper() == "INTEGER" else ""
                rows.append([name, (ctype or "").lower(), "NO" if notnull else "YES",
                             "PRI" if pk else "", default, auto])
            return _result(["Field", "Type", "Null", "Key", "Default", "Extra"], rows)
        if m.group("create"):
            table = m.group("create")
            r = self._raw("SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", [table])
            if not r["rows"]:
                raise ProgrammingError(1146, f"Table '{table}' doesn't exist")
            return _result(["Table", "Create Table"], [[table, r["rows"][0][0]]])
        if m.group("index"):
            table = m.group("index")
            rows = []
            for _seq, name, unique, _origin, _partial in self._raw(f'PRAGMA index_list("{table}")')["rows"]:
                for seqno, _cid, colname in self._raw(f'PRAGMA index_info("{name}")')["rows"]:
                    rows.append([table, 0 if unique else 1, name, seqno + 1, colname])
            return _result(["Table", "Non_unique", "Key_name", "Seq_in_index", "Column_name"], rows)
        if m.group("version"):
            return _result(["VERSION()"], [[f"8.0.0-pys3rds-{__version__}"]])
        if m.group("database"):
            return _result(["DATABASE()"], [[self.database]])
        raise InternalError(0, "unreachable")

    def _list_databases(self):
        """GET /api/databases on a short-lived connection (the keep-alive path is POST-only)."""
        import http.client
        import json
        conn = http.client.HTTPSConnection(self._host, timeout=self.timeout)
        try:
            conn.request("GET", self._base_path + "/api/databases",
                         headers={"authorization": f"Basic {self._auth}"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
            if resp.status >= 400:
                raise pys3sqlite.Error(resp.status, data.get("error", ""))
            return data.get("databases", [])
        finally:
            conn.close()


SrdsClient = Connection


def connect(host, user="", password="", database="", timeout=30,
            cursorclass=Cursor, **_ignored):
    """Same signature as pymysql.connect; extra pymysql arguments are accepted and ignored."""
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, database, user, password, timeout, cursorclass)
