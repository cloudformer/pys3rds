"""Stage the server for Lambda: the pip package s3rds[server] (s3rds.server + the pinned SQLite engine) as Linux wheels.

    python3 scripts/build_server.py "s3rds[server]==0.3.1"   -> terraform/.build/db/  (a release from PyPI)
    python3 scripts/build_server.py "./client[server]"       -> the same from this working tree (before a release)

Terraform runs it itself at plan time with server_package. A path is relative to the repo root.
Prints {"dir": "<path>"} for Terraform's external data source.
"""
import glob
import hashlib
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


def fingerprint(spec, path):
    """The spec plus, for a source tree, every file in it: unchanged -> reuse OUT as it is (a reinstall gives new
    file times, so a new zip hash and a Lambda update on every plan)."""
    h = hashlib.sha256(spec.encode())
    tree = os.path.join(ROOT, path)
    if os.path.isdir(tree):
        for d, dirs, files in sorted(os.walk(tree)):
            dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", "build", "dist") and not x.endswith(".egg-info"))
            for f in sorted(files):
                full = os.path.join(d, f)
                h.update(os.path.relpath(full, tree).encode())
                with open(full, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def main():
    spec = sys.argv[1]
    path, _, extras = spec.partition("[")
    stamp, want = OUT + ".stamp", fingerprint(spec, path)
    if os.path.isdir(OUT) and os.path.isfile(stamp) and open(stamp, encoding="utf-8").read() == want:
        print(json.dumps({"dir": OUT}))
        return
    if os.path.isdir(os.path.join(ROOT, path)):  # a source tree: build its wheel first (--only-binary needs one)
        shutil.rmtree(DIST, ignore_errors=True)
        pip("wheel", "--no-deps", "-w", DIST, path)
        spec = glob.glob(os.path.join(DIST, "*.whl"))[0] + ("[" + extras if extras else "")
    shutil.rmtree(OUT, ignore_errors=True)
    pip("install", "--target", OUT, "--no-compile", *PLATFORM, spec)  # no .pyc: they embed timestamps, the zip hash would change every plan
    shutil.rmtree(os.path.join(OUT, "bin"), ignore_errors=True)
    with open(stamp, "w", encoding="utf-8") as fh:
        fh.write(want)
    print(json.dumps({"dir": OUT}))


if __name__ == "__main__":
    main()
