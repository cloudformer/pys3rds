"""pys3rds — a serverless RDS on your own AWS account (Lambda URL endpoint, SQLite on S3).

Drivers are top-level modules named after the dialect / standard client they mirror,
so switching is always a one-line import change:

    import pys3sqlite           # SQLite dialect, pymysql-shaped API     (also: from pys3rds import pys3sqlite)
    import pys3mysql            # MySQL dialect, pymysql-shaped API      (pip install "pys3rds[mysql]")
    # import pys3pgsql          # PostgreSQL dialect, psycopg2-shaped    (planned)
"""
import pys3sqlite  # noqa: F401
import pys3mysql   # noqa: F401  (sqlglot is only needed when a MySQL statement is actually translated)

__all__ = ["pys3sqlite", "pys3mysql"]
__version__ = pys3sqlite.__version__
