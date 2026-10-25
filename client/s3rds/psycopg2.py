"""s3rds.psycopg2 — the PostgreSQL-dialect driver for s3rds. Planned; not implemented yet.

It will mirror psycopg2 (connect(dsn or host=/user=/password=/dbname=), cursors, %s placeholders)
and translate PostgreSQL SQL to SQLite on the client, the same way s3rds.pymysql does for MySQL.
"""
apilevel = "2.0"
threadsafety = 1
paramstyle = "pyformat"


class Error(Exception):
    pass


class NotSupportedError(Error):
    pass


def connect(*args, **kwargs):
    raise NotSupportedError(
        "s3rds.psycopg2 is not implemented yet. Use `from s3rds import pymysql` (MySQL dialect) "
        "or `from s3rds import sqlite` (SQLite dialect) for now.")
