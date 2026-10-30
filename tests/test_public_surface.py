"""The public surface only grows (CLAUDE.md, principle 12): an upgrade never breaks a user.

Each list below is what users already rely on. Adding to the package is free; removing or renaming anything
listed here fails this test. Extend a list when you add something public; never shorten one.
"""
import importlib
import importlib.util
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXTRAS = {"mysql", "postgres", "server", "sqlalchemy", "django"}
ENTRY_POINTS = {'"sqlite.s3rds" = "s3rds.sqlite:SQLAlchemyDialect"'}
IMPORTS = {  # module -> names it must provide; the module's own extra must be installed
    "s3rds.sqlite": ["connect", "Cursor", "DictCursor", "Error"],  # SQLAlchemyDialect: the entry point above
    "s3rds.pymysql": ["connect", "cursors", "Error", "IntegrityError", "OperationalError", "NotSupportedError"],
    "s3rds.psycopg2": ["connect", "extras", "errors", "extensions", "Error", "IntegrityError"],
    "s3rds.server.lambda_function": ["lambda_handler"],
}
ROUTES = [  # (method, an example path)
    ("GET", "/api/databases"),
    ("POST", "/api/databases"),
    ("GET", "/api/db/d/tables"),
    ("GET", "/api/db/d/table/t"),
    ("POST", "/api/db/d/query"),
    ("POST", "/api/db/d/tx"),
    ("POST", "/api/db/d/replay"),
]
TERRAFORM_KEYS = {
    "region", "aws_profile", "assume_role_arn", "env", "project_name", "console", "function_url_auth",
    "console_auth_username", "console_auth_password", "db_ro", "force_destroy",
}


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def test_pip_extras_and_entry_points():
    toml = _read("client", "pyproject.toml")  # no tomllib on Python 3.9 / 3.10
    section = re.search(r"^\[project\.optional-dependencies\]\n(.*?)(?=^\[)", toml, re.M | re.S).group(1)
    extras = set(re.findall(r"^([A-Za-z0-9_-]+)\s*=", section, re.M))
    assert EXTRAS <= extras, f"extras removed: {EXTRAS - extras}"
    for entry in ENTRY_POINTS:
        assert entry in toml, f"entry point removed: {entry}"


@pytest.mark.parametrize("module", sorted(IMPORTS))
def test_import_paths(module):
    needs = {"s3rds.pymysql": "sqlglot", "s3rds.psycopg2": "sqlglot"}.get(module)
    if needs:
        pytest.importorskip(needs)
    mod = importlib.import_module(module)
    missing = [name for name in IMPORTS[module] if not hasattr(mod, name)]
    assert not missing, f"{module} lost {missing}"


def test_django_engine_module():
    pytest.importorskip("django")
    assert importlib.util.find_spec("s3rds.sqlite.base"), 'ENGINE "s3rds.sqlite" needs s3rds/sqlite/base.py'


def test_http_routes():
    from s3rds.server import lambda_function as lf
    for method, path in ROUTES:
        assert any(m == method and p.fullmatch(path) for m, p, _ in lf.ROUTES), f"route removed: {method} {path}"


def test_terraform_keys():
    declared = set(re.findall(r'^variable "([a-z_]+)"', _read("terraform", "variables.tf"), re.M))
    assert TERRAFORM_KEYS <= declared, f"var-file keys removed: {TERRAFORM_KEYS - declared}"
