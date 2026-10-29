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
from . import sqlite  # noqa: F401
from . import pymysql  # noqa: F401  (sqlglot is only needed when a MySQL statement is actually translated)
from . import psycopg2  # noqa: F401

__all__ = ["sqlite", "pymysql", "psycopg2"]
__version__ = sqlite.__version__
