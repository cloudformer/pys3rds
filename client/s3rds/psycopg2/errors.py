"""s3rds.psycopg2.errors — psycopg2.errors: one class per SQLSTATE, under the DB-API classes (only those s3rds raises)."""
from . import (DataError, Error, IntegrityError, InternalError, NotSupportedError, OperationalError,
               ProgrammingError)


class TransactionRollbackError(OperationalError): pass


class UniqueViolation(IntegrityError): sqlstate = "23505"
class NotNullViolation(IntegrityError): sqlstate = "23502"
class ForeignKeyViolation(IntegrityError): sqlstate = "23503"
class CheckViolation(IntegrityError): sqlstate = "23514"
class InvalidTextRepresentation(DataError): sqlstate = "22P02"
class UndefinedTable(ProgrammingError): sqlstate = "42P01"
class UndefinedColumn(ProgrammingError): sqlstate = "42703"
class DuplicateTable(ProgrammingError): sqlstate = "42P07"
class UndefinedFunction(ProgrammingError): sqlstate = "42883"
class SyntaxError(ProgrammingError): sqlstate = "42601"  # noqa: A001  (psycopg2's name)
class InvalidCatalogName(ProgrammingError): sqlstate = "3D000"
class FeatureNotSupported(NotSupportedError): sqlstate = "0A000"
class ReadOnlySqlTransaction(InternalError): sqlstate = "25006"
class SerializationFailure(TransactionRollbackError): sqlstate = "40001"


_BY_CODE = {c.sqlstate: c for c in list(globals().values())
            if isinstance(c, type) and issubclass(c, Error) and c.sqlstate}


def lookup(code):
    """The class for a SQLSTATE, as psycopg2.errors.lookup."""
    return _BY_CODE[code]
