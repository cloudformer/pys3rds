"""s3rds — a serverless RDS on your own AWS account (Lambda URL endpoint, SQLite on S3).

Each driver is a submodule named after the standard client it mirrors, so the day you move to a
real database you change one import line and nothing else:

    from s3rds import pymysql        # today: MySQL dialect on the s3rds endpoint
    import pymysql                   # later: a real RDS MySQL -- pymysql.connect(...), pymysql.cursors, errors all unchanged

    from s3rds import sqlite         # SQLite dialect, no translation, standard library only
    from s3rds import psycopg2       # PostgreSQL dialect; s3rds.psycopg2.extras / .errors / .extensions as psycopg2's

SQLAlchemy (Core / ORM) needs no import: create_engine("sqlite+s3rds://user:password@<lambda-url>/<db>")
uses SQLAlchemy's SQLite dialect with s3rds.sqlite as its driver; on RDS the URL becomes mysql+pymysql://.
Django needs no import either: DATABASES ENGINE "s3rds.sqlite" (s3rds/sqlite/base.py) is Django's SQLite
backend on s3rds.sqlite; on RDS the ENGINE becomes django.db.backends.mysql.
"""
from . import sqlite  # noqa: F401  (standard library only)

__all__ = ["sqlite", "pymysql", "psycopg2"]
__version__ = sqlite.__version__


def __getattr__(name):
    """s3rds.pymysql / s3rds.psycopg2 load on first use: they translate with sqlglot (extras "mysql" / "postgres"),
    which s3rds.sqlite, SQLAlchemy, Django and the server (s3rds.server) never need."""
    if name in ("pymysql", "psycopg2"):
        import importlib
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
