"""pys3rds — serverless RDS on your own AWS account (SQLite on S3, Lambda on demand).

Drivers are exposed as top-level modules named after the standard client they mirror,
so switching to the real thing is a one-line import change:

    import pys3mysql            # pymysql-shaped   (also: from pys3rds import pys3mysql)
    # import pys3pgsql          # psycopg2-shaped, planned
"""
import pys3mysql  # noqa: F401  (binds the driver as pys3rds.pys3mysql)

__all__ = ["pys3mysql"]
__version__ = pys3mysql.__version__
