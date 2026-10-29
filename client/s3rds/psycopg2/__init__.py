"""s3rds.psycopg2 — the PostgreSQL-dialect driver for s3rds. Write PostgreSQL; it runs on the s3rds endpoint (SQLite engine).

    from s3rds import psycopg2             # later: `import psycopg2` on a real RDS, nothing else changes
    from s3rds.psycopg2.extras import RealDictCursor
    conn = psycopg2.connect(host="xxx.lambda-url.us-east-2.on.aws", dbname="blog", user="admin", password="...")
    with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:      # `with conn`: commit, or rollback on error
        cur.execute("CREATE TABLE users (id SERIAL PRIMARY KEY, name VARCHAR(50) NOT NULL)")
        cur.execute("INSERT INTO users (name) VALUES (%s) RETURNING id", ["alice"])
        cur.execute("SELECT * FROM users WHERE name = %(n)s", {"n": "alice"})

Translated by sqlglot (client side, cached per statement), plus: SERIAL / IDENTITY PRIMARY KEY -> INTEGER PRIMARY KEY
AUTOINCREMENT, declared column types kept (DATE / TIMESTAMP / NUMERIC come back as date / datetime / Decimal),
TRUNCATE, START TRANSACTION. Errors are psycopg2's classes with the SQLSTATE in pgcode (s3rds.psycopg2.errors).
Transactions as psycopg2: autocommit=False by default, commit() / rollback(); a transaction opens at the first write.

Not supported, raised as NotSupportedError: functions / procedures / triggers, LISTEN / NOTIFY, COPY, row and table
locks, roles / GRANT, sequences, arrays, extensions, temporary tables. Known gaps: tests/compat/TASKS.md.
"""
import functools
import re

from .. import sqlite as _sqlite
from ..sqlite import Cursor

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


# ---------------- exceptions: psycopg2's classes, pgcode = SQLSTATE ----------------

class Warning(Exception):  # noqa: A001  (DB-API name)
    pass


class Error(Exception):
    sqlstate = None

    def __init__(self, message=""):
        super().__init__(message)
        self.pgcode = self.sqlstate
        self.pgerror = f"ERROR:  {message}\n"


class InterfaceError(Error): pass
class DatabaseError(Error): pass
class DataError(DatabaseError): pass
class OperationalError(DatabaseError): pass
class IntegrityError(DatabaseError): pass
class InternalError(DatabaseError): pass
class ProgrammingError(DatabaseError): pass
class NotSupportedError(DatabaseError): pass


from . import errors, extensions, extras  # noqa: E402,F401  (errors needs the classes above)

# server message -> the psycopg2.errors class PostgreSQL would have raised (its sqlstate is the pgcode)
_SQLITE_TO_PG = [
    (r"UNIQUE constraint failed",      errors.UniqueViolation),
    (r"NOT NULL constraint failed",    errors.NotNullViolation),
    (r"FOREIGN KEY constraint failed", errors.ForeignKeyViolation),
    (r"CHECK constraint failed",       errors.CheckViolation),
    (r"no such table",                 errors.UndefinedTable),
    (r"no such column",                errors.UndefinedColumn),
    (r"already exists",                errors.DuplicateTable),
    (r"no such function",              errors.UndefinedFunction),
    (r"syntax error|incomplete input", errors.SyntaxError),
    (r"readonly|read-only",            errors.ReadOnlySqlTransaction),
]


def _map_error(e):
    """s3rds.sqlite.Error (HTTP status + server message) -> psycopg2-style exception."""
    msg = e.message or str(e)
    if e.status == 0:
        return OperationalError(f"server closed the connection: {msg}")
    if e.status == 401:
        return OperationalError("password authentication failed")
    if e.status == 404 and "database" in msg:
        return errors.InvalidCatalogName(msg)
    if e.status == 409:
        return errors.SerializationFailure(f"could not serialize access due to concurrent update ({msg})")
    for pattern, cls in _SQLITE_TO_PG:
        if re.search(pattern, msg, re.I):
            return cls(msg)
    return InternalError(msg)


# ---------------- placeholders: %s / %(name)s -> ? (psycopg2 formats the whole string, quotes included) ----------------

def _bind(sql, params):
    if not params:
        return sql, []
    names = []

    def sub(m):
        if m.group(0) == "%%":
            return "%"
        names.append(m.group(1))
        return "?"
    out = re.sub(r"%%|%\((\w+)\)s|%s", sub, sql)
    if isinstance(params, dict):
        return out, [params[k] for k in names]
    params = list(params)
    if len(params) != len(names):
        raise TypeError("not all arguments converted during string formatting" if len(params) > len(names)
                        else "not enough arguments for format string")
    return out, params


# ---------------- translation: sqlglot postgres -> sqlite, plus what it does not do ----------------

_UNSUPPORTED = [
    (r"^\s*CREATE\s+(OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|TRIGGER|RULE)\b|^\s*(DO|CALL)\b", "functions / procedures / triggers"),
    (r"^\s*(LISTEN|NOTIFY|UNLISTEN)\b", "LISTEN / NOTIFY"),
    (r"^\s*COPY\b", "COPY"),
    (r"^\s*LOCK\b|\bFOR\s+(NO\s+KEY\s+)?(UPDATE|SHARE)\b", "row and table locks"),
    (r"^\s*(GRANT|REVOKE)\b|^\s*(CREATE|ALTER|DROP)\s+(ROLE|USER|GROUP)\b", "roles / GRANT / REVOKE"),
    (r"^\s*(CREATE|ALTER|DROP)\s+(SEQUENCE|EXTENSION|SCHEMA|TYPE|DOMAIN|DATABASE)\b|\b(nextval|currval|setval)\s*\(",
     "sequences / extensions / schemas / types"),
    (r"^\s*CREATE\s+(TEMP|TEMPORARY|UNLOGGED)\b", "temporary tables"),
    (r"\bARRAY\s*\[|\w\s*\[\]", "arrays"),
]
_STATEMENTS = ("Select", "Union", "Intersect", "Except", "Insert", "Update", "Delete", "Create", "Drop", "Alter")
_TX = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT|SAVEPOINT|RELEASE)\b", re.I)
_TRUNCATE = re.compile(r"^\s*TRUNCATE\s+(?:TABLE\s+)?(?:ONLY\s+)?([\w\s,\"]+?)(\s+RESTART\s+IDENTITY)?"
                       r"(?:\s+CONTINUE\s+IDENTITY)?\s*$", re.I)
_SERIAL = {"SERIAL", "BIGSERIAL", "SMALLSERIAL"}


def _fix(node):
    if isinstance(node, exp.ColumnDef) and node.args.get("kind") is not None:
        kind, constraints = node.args["kind"], node.args.get("constraints") or []
        if kind.this.name in _SERIAL or any(isinstance(c.kind, exp.GeneratedAsIdentityColumnConstraint) for c in constraints):
            if not any(isinstance(c.kind, exp.PrimaryKeyColumnConstraint) for c in constraints):
                raise errors.FeatureNotSupported("SERIAL / IDENTITY on a column that is not its PRIMARY KEY")
            node.set("kind", exp.DataType.build("INTEGER"))
            others = [c for c in constraints if not isinstance(
                c.kind, (exp.PrimaryKeyColumnConstraint, exp.GeneratedAsIdentityColumnConstraint))]
            node.set("constraints", others + [exp.ColumnConstraint(kind=exp.PrimaryKeyColumnConstraint()),
                                              exp.ColumnConstraint(kind=exp.AutoIncrementColumnConstraint())])
        else:  # keep the declared type: the server returns DATE / TIMESTAMP / NUMERIC columns typed by it
            node.set("kind", exp.DataType(this=exp.DataType.Type.USERDEFINED, kind=kind.sql(dialect="postgres")))
    elif isinstance(node, exp.OnConflict):  # sqlglot writes ON CONFLICT (a NULLS LAST), which SQLite rejects
        node.set("conflict_keys", [k.this if isinstance(k, exp.Ordered) else k for k in node.args.get("conflict_keys") or []])
    elif isinstance(node, exp.Table) and node.db == "public":
        node.set("db", None)
    return node


@functools.lru_cache(maxsize=1024)
def _translate(sql):
    """PostgreSQL statement -> tuple of SQLite statements."""
    for pattern, what in _UNSUPPORTED:
        if re.search(pattern, sql, re.I | re.S):
            raise errors.FeatureNotSupported(f"{what} need a real PostgreSQL - move to RDS when you do")
    if _TX.match(sql):
        sql = re.sub(r"^\s*ABORT\b", "ROLLBACK", sql, flags=re.I)
        return (re.sub(r"^\s*(START\s+TRANSACTION|BEGIN)\b.*", "BEGIN", sql, flags=re.I | re.S),)
    m = _TRUNCATE.match(sql)
    if m:
        tables = [t.strip().strip('"') for t in m.group(1).split(",")]
        return tuple([f'DELETE FROM "{t}"' for t in tables]
                     + ([f"DELETE FROM sqlite_sequence WHERE name = '{t}'" for t in tables] if m.group(2) else []))
    if sqlglot is None:
        raise NotSupportedError("the PostgreSQL dialect needs sqlglot (an s3rds dependency): pip install s3rds")
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except ParseError as e:
        raise errors.SyntaxError(f"syntax error ({e})") from None
    if type(tree).__name__ not in _STATEMENTS:
        raise errors.FeatureNotSupported(f"{type(tree).__name__} statements are not supported by s3rds.psycopg2")
    return (tree.transform(_fix).sql(dialect="sqlite"),)


# ---------------- connection ----------------

class Connection(_sqlite.Connection):
    _typed = True    # the server marks DATE / TIME / TIMESTAMP / NUMERIC columns by their declared type
    _closed = False

    @property
    def autocommit(self):
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value):
        if self._tx is not None:
            raise ProgrammingError("set_session cannot be used inside a transaction")
        self._autocommit = bool(value)

    @property
    def closed(self):
        return int(self._closed)

    def cursor(self, name=None, cursor_factory=None, **_ignored):
        if self._closed:
            raise InterfaceError("connection already closed")
        return (cursor_factory or self.cursorclass)(self)

    def close(self):
        self._closed = True
        super().close()

    def __exit__(self, exc_type, *exc):   # psycopg2: `with conn` ends the transaction, it does not close
        if exc_type is None:
            self.commit()
        else:
            self.rollback()

    def rollback(self):
        self._written = False   # no warning: PostgreSQL cannot undo autocommitted statements either
        super().rollback()

    def execute(self, sql, params=()):
        if self._closed:
            raise InterfaceError("connection already closed")
        sql = sql.decode() if isinstance(sql, bytes) else sql
        stmts, args = self._prepare(sql, params)
        try:
            if len(stmts) == 1:
                r = super().execute(stmts[0], args)
            elif self._tx is not None or not self._autocommit:
                self._tx = self._tx or {"log": [], "base": None}
                r = self._tx_run([(s, []) for s in stmts])[0]
            else:
                r = self.transaction([(s, []) for s in stmts])[0]
        except _sqlite.Error as e:
            raise _map_error(e) from None
        r["lastrowid"] = 0      # psycopg2: the OID, 0 without OIDs; use INSERT ... RETURNING
        if r.get("columns"):
            r["rowcount"] = len(r.get("rows") or [])
        elif len(stmts) > 1 or _TX.match(sql):
            r["rowcount"] = -1   # TRUNCATE and transaction statements report no row count
        return r

    def _prepare(self, sql, params):
        qmark, args = _bind(sql.strip().rstrip(";"), params)
        return _translate(qmark), args

    def _executemany(self, sql, seq_of_params):
        prepared = []
        for params in seq_of_params:
            stmts, args = self._prepare(sql, params)
            prepared += [(stmts[0], args)] + [(s, []) for s in stmts[1:]]
        if not prepared:
            return []
        try:
            if self._tx is None and (self._autocommit or not _sqlite._IMPLICIT_BEGIN.match(prepared[0][0])):
                return self.transaction(prepared)
            self._tx = self._tx or {"log": [], "base": None}
            return self._tx_run(prepared)
        except _sqlite.Error as e:
            raise _map_error(e) from None


def connect(dsn=None, connection_factory=None, cursor_factory=None, **kwargs):
    """psycopg2.connect: a DSN ("host=... dbname=..." or postgresql://user:password@host/dbname) and/or keywords.
    host is the Lambda Function URL; port, sslmode and the other libpq options are accepted and ignored."""
    params = extensions.parse_dsn(dsn) if dsn else {}
    params.update({k: v for k, v in kwargs.items() if v is not None})
    host = params.get("host")
    if not host:
        raise OperationalError("s3rds.psycopg2 needs host=<lambda-url>")
    url = host if host.startswith("http") else "https://" + host
    return Connection(url, params.get("dbname") or params.get("database", ""), params.get("user", ""),
                      params.get("password", ""), int(params.get("connect_timeout") or 30),
                      cursor_factory or Cursor, bool(params.get("autocommit", False)), "qmark")
