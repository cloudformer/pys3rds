"""Stage the server for Lambda: src/db plus its pinned dependencies (src/db/requirements.txt) as Linux wheels.

    python3 scripts/build_server.py        -> terraform/.build/db/  (Terraform runs it itself, at plan time;
                                              CloudFormation: run it first, the template packages that folder)

Prints {"dir": "<path>"} for Terraform's external data source. pip downloads the wheels once; a later run
only re-copies the source.
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src", "db")
OUT = os.path.join(ROOT, "terraform", ".build", "db")
DEPS = os.path.join(ROOT, "terraform", ".build", "db-deps")
# the Lambda: runtime python3.12, architecture x86_64 (terraform/main.tf)
PLATFORM = ["--platform", "manylinux_2_28_x86_64", "--implementation", "cp", "--python-version", "3.12",
            "--only-binary=:all:"]


def main():
    req = os.path.join(SRC, "requirements.txt")
    with open(req, encoding="utf-8") as f:
        wanted = f.read()
    stamp = os.path.join(DEPS, ".requirements")
    if not (os.path.isfile(stamp) and open(stamp, encoding="utf-8").read() == wanted):
        shutil.rmtree(DEPS, ignore_errors=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "--target", DEPS, *PLATFORM, "-r", req], capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"build_server: pip failed: {r.stderr.strip()[-500:]}")
        with open(stamp, "w", encoding="utf-8") as f:
            f.write(wanted)
    shutil.rmtree(OUT, ignore_errors=True)
    shutil.copytree(SRC, OUT, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "requirements.txt"))
    shutil.copytree(DEPS, OUT, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".requirements", "*.dist-info", "bin"))
    print(json.dumps({"dir": OUT}))


if __name__ == "__main__":
    main()
