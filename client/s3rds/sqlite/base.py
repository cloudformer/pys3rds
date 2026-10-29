"""Django on s3rds: ENGINE "s3rds.sqlite" -- Django's own SQLite backend with s3rds.sqlite as its driver.

    DATABASES = {"default": {
        "ENGINE": "s3rds.sqlite",                     # was "django.db.backends.sqlite3"
        "NAME": "blog",                               # the s3rds database (create it once in the console)
        "HOST": "xxx.lambda-url.us-east-2.on.aws",
        "USER": "admin",
        "PASSWORD": "...",
    }}

Django writes the same SQLite SQL as for a local file; only what cannot run over a server that keeps no session
and runs no Python is changed here:
- the connection is s3rds.sqlite: Django's transactions (atomic, savepoints) on the driver's open transactions,
  PRAGMA foreign_keys (migrations turn it off) on the driver's connection settings;
- Django's SQLite backend registers ~35 Python functions on every connection. The date ones (Extract, Trunc,
  __year, __date, ...) are written here with SQLite's own strftime(); the rest would need Python on the server
  and raise NotSupportedError naming the feature: time-zone conversion in queries (USE_TZ with a TIME_ZONE
  other than the connection's), regex lookups, MD5 / SHA*, LPad / RPad / Repeat / Reverse / Cot / Random
  (order_by("?")), StdDev / Variance, duration arithmetic (F("dt") + timedelta, F("end") - F("start")).
Imported by Django (ENGINE), never by `import s3rds`; Django is the application's.
"""
import re
from collections.abc import Mapping

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import NotSupportedError, models
from django.db.backends.sqlite3 import base, client, creation, features, operations
from django.db.models import functions
from django.utils.asyncio import async_unsafe

from s3rds import sqlite as Database

_NO_PYTHON = "Django's SQLite backend runs it as a Python function on the connection; s3rds runs no Python " \
             "on the server (move to RDS for it)"


class DatabaseFeatures(features.DatabaseFeatures):
    # the parent reads the version of the local sqlite3 module; the engine is the server's
    supports_aggregate_order_by_clause = Database.sqlite_version_info >= (3, 44, 0)
    can_clone_databases = False
    # Python functions in Django's SQLite backend: Django refuses these features itself
    supports_any_value = False
    supports_uuid4_function = False

    @property
    def supports_uuid7_function(self):
        return False


# Django functions / aggregates that are Python functions on a sqlite3 connection (django/db/backends/sqlite3/_functions.py)
_PYTHON_FUNCTIONS = tuple(c for c in (
    functions.MD5, functions.SHA1, functions.SHA224, functions.SHA256, functions.SHA384, functions.SHA512,
    functions.LPad, functions.RPad, functions.Repeat, functions.Reverse, functions.Cot, functions.Random,
    models.StdDev, models.Variance,
    getattr(models, "BitAnd", None), getattr(models, "BitOr", None), getattr(models, "BitXor", None),
) if c is not None)

# Extract(lookup) -> strftime format; the value is the integer Django's Python function returns
_EXTRACT = {"year": "%Y", "month": "%m", "day": "%d", "hour": "%H", "minute": "%M", "second": "%S",
            "week": "%V", "iso_year": "%G", "iso_week_day": "%u"}
# Trunc(lookup) on a DateField / DateTimeField / TimeField -> strftime format (+ modifiers)
_TRUNC_DATE = {"year": "%Y-01-01", "month": "%Y-%m-01", "week": "%Y-%m-%d", "day": "%Y-%m-%d"}
_TRUNC_DATETIME = {"year": "%Y-01-01 00:00:00", "month": "%Y-%m-01 00:00:00", "week": "%Y-%m-%d 00:00:00",
                   "day": "%Y-%m-%d 00:00:00", "hour": "%Y-%m-%d %H:00:00", "minute": "%Y-%m-%d %H:%M:00",
                   "second": "%Y-%m-%d %H:%M:%S"}
_TRUNC_TIME = {"hour": "%H:00:00", "minute": "%H:%M:00", "second": "%H:%M:%S"}


class DatabaseOperations(operations.DatabaseOperations):
    """Django's SQLite operations with the date functions in SQLite's own SQL (no django_* Python functions).
    Formats travel as parameters, as Django passes its lookup names, so % never appears in the SQL."""

    def check_expression_support(self, expression):
        if isinstance(expression, _PYTHON_FUNCTIONS):
            raise NotSupportedError(f"{type(expression).__name__}(): {_NO_PYTHON}")
        super().check_expression_support(expression)

    def _no_tz_conversion(self, tzname):
        if tzname and settings.USE_TZ and tzname != self.connection.timezone_name:
            raise NotSupportedError(
                f"time-zone conversion in a query ({self.connection.timezone_name} -> {tzname}): {_NO_PYTHON}. "
                f"Use TIME_ZONE = '{self.connection.timezone_name}', or timezone.override() around the query")

    def _extract(self, lookup_type, sql, params):
        lookup_type = lookup_type.lower()
        if lookup_type == "week_day":  # Sunday = 1 ... Saturday = 7
            return f"(CAST(strftime(%s, {sql}) AS INTEGER) + 1)", ("%w", *params)
        if lookup_type == "quarter":
            return f"((CAST(strftime(%s, {sql}) AS INTEGER) + 2) / 3)", ("%m", *params)
        if lookup_type not in _EXTRACT:
            raise ValueError(f"Unsupported lookup type: {lookup_type!r}")
        return f"CAST(strftime(%s, {sql}) AS INTEGER)", (_EXTRACT[lookup_type], *params)

    def _trunc(self, formats, suffix, lookup_type, sql, params):
        lookup_type = lookup_type.lower()
        if lookup_type == "quarter":  # the first month of the quarter
            return (f"(strftime(%s, {sql}) || printf(%s, (CAST(strftime(%s, {sql}) AS INTEGER) - 1) / 3 * 3 + 1)"
                    f" || %s)", ("%Y-", *params, "%02d", "%m", *params, "-01" + suffix))
        if lookup_type not in formats:
            raise ValueError(f"Unsupported lookup type: {lookup_type!r}")
        if lookup_type == "week":  # back to Monday: forward to Sunday, then six days back
            return f"strftime(%s, {sql}, 'weekday 0', '-6 days')", (formats["week"], *params)
        return f"strftime(%s, {sql})", (formats[lookup_type], *params)

    def date_extract_sql(self, lookup_type, sql, params):
        return self._extract(lookup_type, sql, params)

    def datetime_extract_sql(self, lookup_type, sql, params, tzname):
        self._no_tz_conversion(tzname)
        return self._extract(lookup_type, sql, params)

    def time_extract_sql(self, lookup_type, sql, params):
        return self._extract(lookup_type, sql, params)

    def date_trunc_sql(self, lookup_type, sql, params, tzname=None):
        self._no_tz_conversion(tzname)
        return self._trunc(_TRUNC_DATE, "", lookup_type, sql, params)

    def datetime_trunc_sql(self, lookup_type, sql, params, tzname):
        self._no_tz_conversion(tzname)
        return self._trunc(_TRUNC_DATETIME, " 00:00:00", lookup_type, sql, params)

    def time_trunc_sql(self, lookup_type, sql, params, tzname=None):
        self._no_tz_conversion(tzname)
        return self._trunc(_TRUNC_TIME, "", lookup_type, sql, params)

    def datetime_cast_date_sql(self, sql, params, tzname):
        self._no_tz_conversion(tzname)
        return f"date({sql})", params

    def datetime_cast_time_sql(self, sql, params, tzname):
        self._no_tz_conversion(tzname)
        # time() drops the microseconds; Django's value keeps them when there are any (HH:MM:SS[.ffffff])
        return (f"(time({sql}) || CASE WHEN substr({sql}, 20, 1) = '.' THEN substr({sql}, 20, 7) ELSE '' END)",
                (*params, *params, *params))

    def combine_expression(self, connector, sub_expressions):
        if connector == "#":
            raise NotSupportedError(f"bitwise XOR (BITXOR): {_NO_PYTHON}")
        return super().combine_expression(connector, sub_expressions)  # ^ is POWER(), built into the engine

    def combine_duration_expression(self, connector, sub_expressions):
        raise NotSupportedError(f"date / time arithmetic with a timedelta: {_NO_PYTHON}")

    def subtract_temporals(self, internal_type, lhs, rhs):
        raise NotSupportedError(f"subtracting dates / times: {_NO_PYTHON}")

    def regex_lookup(self, lookup_type):
        raise NotSupportedError(f"__{lookup_type} lookups (REGEXP): {_NO_PYTHON}")

    def _quote_params_for_last_executed_query(self, params):
        """connection.queries with DEBUG: quote here as SQLite's QUOTE() would, instead of one more request per query."""
        return tuple(_quote(p) for p in params)


def _quote(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"X'{bytes(value).hex().upper()}'"
    return "'" + str(value).replace("'", "''") + "'"


class DatabaseCreation(creation.DatabaseCreation):
    def _create_test_db(self, verbosity, autoclobber, keepdb=False):
        raise NotSupportedError("test databases on s3rds: run the tests on django.db.backends.sqlite3 "
                                "(the same SQL on a local file)")


class DatabaseClient(client.DatabaseClient):
    @classmethod
    def settings_to_cmd_args_env(cls, settings_dict, parameters):
        raise NotSupportedError("dbshell: open the s3rds console (the Function URL in a browser)")


_FORMAT = re.compile(r"(?<!%)%s")
_PYFORMAT = re.compile(r"%\((\w+)\)s")


def _convert(query, params):
    """Django's format (%s) / pyformat (%(name)s) SQL -> ? placeholders and a list, %% -> %
    (as Django's SQLiteCursorWrapper, which turns pyformat into :name for sqlite3)."""
    if isinstance(params, Mapping):
        names = _PYFORMAT.findall(query)
        return _PYFORMAT.sub("?", query).replace("%%", "%"), [params[n] for n in names]
    return _FORMAT.sub("?", query).replace("%%", "%"), params


class CursorWrapper(Database.Cursor):
    """The cursor Django gets; execute() returns the cursor, as sqlite3's does (Django chains .fetchone())."""

    def execute(self, query, params=None):
        if params is not None:
            query, params = _convert(query, params)
        super().execute(query, params)
        return self

    def executemany(self, query, param_list):
        param_list = list(param_list)
        if param_list and isinstance(param_list[0], Mapping):
            names = _PYFORMAT.findall(query)
            query = _PYFORMAT.sub("?", query).replace("%%", "%")
            param_list = [[p[n] for n in names] for p in param_list]
        else:
            query = _FORMAT.sub("?", query).replace("%%", "%")
        super().executemany(query, param_list)
        return self


class DatabaseWrapper(base.DatabaseWrapper):
    display_name = "SQLite on s3rds"
    Database = Database
    client_class = DatabaseClient
    creation_class = DatabaseCreation
    features_class = DatabaseFeatures
    ops_class = DatabaseOperations
    # regex / iregex: REGEXP is a Python function on sqlite3; without them Django asks ops.regex_lookup(), which refuses
    operators = {k: v for k, v in base.DatabaseWrapper.operators.items() if k not in ("regex", "iregex")}

    def get_connection_params(self):
        kwargs = super().get_connection_params()  # NAME required; OPTIONS transaction_mode / init_command read
        for key in ("detect_types", "check_same_thread", "uri"):
            kwargs.pop(key, None)
        s = self.settings_dict
        if not s["HOST"]:
            raise ImproperlyConfigured("settings.DATABASES: HOST is the s3rds Function URL host "
                                       "(xxx.lambda-url.<region>.on.aws)")
        return {**kwargs, "host": s["HOST"] + (f":{s['PORT']}" if s["PORT"] else ""), "user": s["USER"],
                "password": s["PASSWORD"], "database": s["NAME"], "paramstyle": "qmark"}

    @async_unsafe
    def get_new_connection(self, conn_params):
        conn = Database.connect(**conn_params)
        # no register_functions(conn): Python functions would have to run on the server (DatabaseOperations above)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA legacy_alter_table = OFF")
        for init_command in self.init_commands:
            if init_command := init_command.strip():
                conn.execute(init_command)
        return conn

    def create_cursor(self, name=None):
        return self.connection.cursor(CursorWrapper)
