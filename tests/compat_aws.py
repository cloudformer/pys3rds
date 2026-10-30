"""The project deployed on AWS for the compatibility tests — shared across the series, a pytest plugin
loaded by tests/conftest.py (`pytest_plugins = ["compat_docker", "compat_aws"]`). Copy it unchanged; the
project's compat conftest names its output and variables in one `endpoint(...)` call.

    python3 -m pytest tests -q --docker run                     the reference in Docker vs the project's
                                                                lambda_handler, a Python function called in-process
    python3 -m pytest tests -q --docker run --aws <var-file>    the reference in Docker vs the deployed Lambda
                                                                Function URL (HTTPS, the real backend)

<var-file> is the Terraform variable file the stack was deployed with (looked up in terraform/, gitignored):
user and password come from it, the URL from `terraform -chdir=terraform output -json`. Nothing is
deployed or destroyed; the tests use one database of their own on the deployed stack.
"""
import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
aws = {"var_file": None, "url": None, "vars": {}, "outputs": {}}


def pytest_addoption(parser):
    parser.addoption("--aws", metavar="VAR_FILE", default=None,
                     help="compare the reference with the deployed stack instead of the in-process handler; "
                          "VAR_FILE is the Terraform variable file it was deployed with (in terraform/)")


def pytest_configure(config):
    path = config.getoption("--aws", None)
    if not path:
        return
    for p in (os.path.join(ROOT, "terraform", path), path):
        if os.path.isfile(p):
            aws["var_file"] = p
            break
    else:
        pytest.exit(f"--aws: {path} not found (looked in terraform/ and as given)", returncode=4)
    with open(aws["var_file"], encoding="utf-8") as f:
        aws["vars"] = json.load(f)
    if aws["vars"].get("function_url_auth", "NONE") != "NONE":
        pytest.exit(f"--aws: {path} deploys function_url_auth={aws['vars']['function_url_auth']}; the tests sign in "
                    "with user / password, which needs function_url_auth NONE", returncode=4)
    r = subprocess.run(["terraform", f"-chdir={os.path.join(ROOT, 'terraform')}", "output", "-json"],
                       capture_output=True, text=True)
    if r.returncode:
        pytest.exit(f"--aws: terraform output failed: {r.stderr.strip()[:300]}", returncode=4)
    aws["outputs"] = {k: v["value"] for k, v in json.loads(r.stdout or "{}").items()}


def endpoint(config, url, user, password):
    """None without --aws; else {"url", "user", "password"} from the output / variable names given."""
    if not config.getoption("--aws", None):
        return None
    missing = [n for n in (url,) if not aws["outputs"].get(n)] + [n for n in (user, password) if not aws["vars"].get(n)]
    if missing:
        pytest.exit(f"--aws: missing {', '.join(missing)} (terraform output / {os.path.basename(aws['var_file'])}); "
                    "deployed with terraform -chdir=terraform apply?", returncode=4)
    aws["url"] = aws["outputs"][url]
    return {"url": aws["url"], "user": aws["vars"][user], "password": aws["vars"][password]}


def target():
    """What the project side was in this run, for the report."""
    return f"deployed Lambda {aws['url']}" if aws["url"] else "lambda_handler in-process"


def pytest_terminal_summary(terminalreporter):
    if aws["var_file"]:
        terminalreporter.section("aws")
        terminalreporter.write_line(f"compared against {target()} ({os.path.relpath(aws['var_file'], ROOT)})")
