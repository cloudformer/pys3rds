"""Stage the server for Lambda: the pip package s3rds[server] (s3rds.server + the pinned SQLite engine) as Linux wheels.

    python3 scripts/build_server.py "s3rds[server]==0.3.1"   -> terraform/.build/db/  (a release from PyPI)
    python3 scripts/build_server.py "./client[server]"       -> the same from this working tree (before a release)

Terraform runs it itself at plan time with server_package; CloudFormation: run it first, the template
packages that folder. A path is relative to the repo root. Prints {"dir": "<path>"} for Terraform's
external data source.
"""
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "terraform", ".build", "db")
DIST = os.path.join(ROOT, "terraform", ".build", "dist")
# the Lambda: runtime python3.12, architecture x86_64 (terraform/main.tf)
PLATFORM = ["--platform", "manylinux_2_28_x86_64", "--implementation", "cp", "--python-version", "3.12",
            "--only-binary=:all:"]


def pip(*args):
    r = subprocess.run([sys.executable, "-m", "pip", "-q", "--disable-pip-version-check", *args],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"build_server: pip failed: {r.stderr.strip()[-500:]}")


def main():
    spec = sys.argv[1]
    path, _, extras = spec.partition("[")
    if os.path.isdir(os.path.join(ROOT, path)):  # a source tree: build its wheel first (--only-binary needs one)
        shutil.rmtree(DIST, ignore_errors=True)
        pip("wheel", "--no-deps", "-w", DIST, path)
        spec = glob.glob(os.path.join(DIST, "*.whl"))[0] + ("[" + extras if extras else "")
    shutil.rmtree(OUT, ignore_errors=True)
    pip("install", "--target", OUT, *PLATFORM, spec)
    shutil.rmtree(os.path.join(OUT, "bin"), ignore_errors=True)
    print(json.dumps({"dir": OUT}))


if __name__ == "__main__":
    main()
