"""s3rds.psycopg2.extensions — psycopg2.extensions: isolation level constants and parse_dsn."""
import re
import urllib.parse

ISOLATION_LEVEL_AUTOCOMMIT = 0
ISOLATION_LEVEL_READ_COMMITTED = 1
ISOLATION_LEVEL_REPEATABLE_READ = 2
ISOLATION_LEVEL_SERIALIZABLE = 3   # what s3rds transactions are: a concurrent commit makes them fail


def parse_dsn(dsn):
    """"host=... dbname=..." or postgresql://user:password@host/dbname -> dict."""
    if re.match(r"postgres(ql)?://", dsn):
        u = urllib.parse.urlparse(dsn)
        out = {"host": u.hostname, "user": urllib.parse.unquote(u.username or ""),
               "password": urllib.parse.unquote(u.password or ""), "dbname": u.path.lstrip("/")}
        out.update(urllib.parse.parse_qsl(u.query))
        return {k: v for k, v in out.items() if v}
    return {k: v.strip("'") for k, v in re.findall(r"(\w+)\s*=\s*('[^']*'|\S+)", dsn)}
