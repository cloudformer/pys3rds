"""Optional dependencies: sqlglot is only for the translating drivers (extras "mysql" / "postgres").

Each check runs in a fresh interpreter where importing sqlglot fails, as on a machine that never installed it:
s3rds.sqlite, SQLAlchemy, Django and the server must work; s3rds.pymysql / s3rds.psycopg2 must say which extra.
"""
import os
import subprocess
import sys
import textwrap

import pytest

CLIENT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")

NO_SQLGLOT = f"""
import sys

class _NoSqlglot:  # any import of sqlglot fails, installed or not
    def find_spec(self, name, path=None, target=None):
        if name == "sqlglot" or name.startswith("sqlglot."):
            raise ModuleNotFoundError("No module named 'sqlglot'", name="sqlglot")

sys.meta_path.insert(0, _NoSqlglot())
sys.path.insert(0, {CLIENT!r})
"""


def run(code):
    env = {**os.environ, "DB_BUCKET": "b", "DB_USER": "u", "DB_PASSWORD": "p", "AWS_DEFAULT_REGION": "us-east-2"}
    r = subprocess.run([sys.executable, "-c", NO_SQLGLOT + textwrap.dedent(code)],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_sqlite_driver_and_server_without_sqlglot():
    assert run("""
        import s3rds
        from s3rds import sqlite
        from s3rds.server import lambda_function
        assert "sqlglot" not in sys.modules
        print("ok", s3rds.__version__ == sqlite.__version__)
    """) == "ok True"


def test_sqlalchemy_without_sqlglot():
    pytest.importorskip("sqlalchemy")
    assert run("""
        from sqlalchemy import create_engine
        from sqlalchemy.dialects import registry
        registry.register("sqlite.s3rds", "s3rds.sqlite", "SQLAlchemyDialect")  # the package's entry point
        engine = create_engine("sqlite+s3rds://u:p@x.lambda-url.us-east-2.on.aws/d")
        assert "sqlglot" not in sys.modules
        print(engine.dialect.name, engine.dialect.driver)
    """) == "sqlite s3rds"


def test_django_without_sqlglot():
    pytest.importorskip("django")
    assert run("""
        import django
        from django.conf import settings
        settings.configure(DATABASES={"default": {"ENGINE": "s3rds.sqlite", "NAME": "d",
                                                  "HOST": "x.lambda-url.us-east-2.on.aws", "USER": "u", "PASSWORD": "p"}})
        django.setup()
        from django.db import connections  # django.db.connection is a proxy; the wrapper itself is in connections
        connection = connections["default"]
        assert "sqlglot" not in sys.modules
        print(connection.vendor, type(connection).__module__)
    """) == "sqlite s3rds.sqlite.base"


@pytest.mark.parametrize("driver, extra", [("pymysql", "mysql"), ("psycopg2", "postgres")])
def test_translating_drivers_name_their_extra(driver, extra):
    assert run(f"""
        import s3rds
        for load in (lambda: __import__("s3rds.{driver}"), lambda: getattr(s3rds, "{driver}")):
            try:
                load()
            except ImportError as e:
                assert 'pip install "s3rds[{extra}]"' in str(e), e
            else:
                raise AssertionError("imported without sqlglot")
        print("ok")
    """) == "ok"
