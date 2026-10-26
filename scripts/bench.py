#!/usr/bin/env python3
"""s3rds latency / throughput benchmark (through the s3rds.sqlite driver: the end-to-end latency business code sees).

Everything is discovered automatically; just run it:
  python3 scripts/bench.py
Optional arguments (any order, recognised by shape):
  xxx.json  the var file (default: the json under terraform/ that has db_user)
  a number  iterations per group (default 30)
  anything else  the database name (default benchdb)
e.g. python3 scripts/bench.py terraform/real-srds.json mt5test 100

Discovery:
  endpoint   <- terraform output -raw db_url (run in ../terraform)
  user/pwd   <- db_user / db_password from the var file

Output: for reads and writes, the cold-start first call, min/p50/p95/max, operations per second.
Cleans up its own table (DROP TABLE) at the end; the database file itself belongs to terraform.
Note: the first request usually hits a cold start; it is listed separately and excluded from the stats;
     to measure a pure cold start again, wait 30+ minutes and look at the first-call line.
"""
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TF_DIR = os.path.join(ROOT, "terraform")
sys.path.insert(0, os.path.join(ROOT, "client"))
from s3rds import sqlite  # noqa: E402


def load_conn(varfile=None):
    """endpoint from terraform output, credentials from the var file (given, or discovered)."""
    cfg = None
    if varfile:
        with open(varfile) as fp:
            cfg = json.load(fp)
        if "db_user" not in cfg:
            sys.exit(f"{varfile} has no db_user / db_password")
    else:
        for f in sorted(os.listdir(TF_DIR)):
            if f.endswith(".json"):
                try:
                    with open(os.path.join(TF_DIR, f)) as fp:
                        data = json.load(fp)
                except (json.JSONDecodeError, OSError):
                    continue
                if "db_user" in data:
                    cfg = data
                    break
        if not cfg:
            sys.exit("no var file with db_user under terraform/ (e.g. real-srds.json)")

    r = subprocess.run(["terraform", "output", "-raw", "db_url"],
                       cwd=TF_DIR, capture_output=True, text=True)
    url = r.stdout.strip()
    if r.returncode != 0 or not url.startswith("http"):
        sys.exit(f"cannot read db_url (terraform apply first?):\n{r.stderr.strip()}")
    return url, cfg["db_user"], cfg["db_password"]


def bench(label, fn, n):
    times = []
    for i in range(n):
        t0 = time.perf_counter()
        fn(i)
        times.append((time.perf_counter() - t0) * 1000)
    first, rest = times[0], times[1:] or times
    rest_sorted = sorted(rest)
    p = lambda q: rest_sorted[min(len(rest_sorted) - 1, int(len(rest_sorted) * q))]
    print(f"{label:<14} first {first:7.0f}ms | "
          f"min {min(rest):6.0f}  p50 {p(.5):6.0f}  p95 {p(.95):6.0f}  "
          f"max {max(rest):6.0f}ms | {1000 / statistics.mean(rest):5.1f} ops/s")


def main():
    varfile, dbname, n = None, "benchdb", 30
    for a in sys.argv[1:]:
        if a.endswith(".json"):
            varfile = a
        elif a.isdigit():
            n = int(a)
        else:
            dbname = a

    url, user, password = load_conn(varfile)
    print(f"endpoint: {url}\ndatabase: {dbname}  user: {user}  serial x{n}\n")

    db = sqlite.connect(host=url, user=user, password=password, database=dbname)
    db.create_database()
    db.execute("CREATE TABLE IF NOT EXISTS bench "
               "(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    size = db.query("SELECT page_count*page_size/1024.0 AS kb "
                    "FROM pragma_page_count(), pragma_page_size()")[0]["kb"]
    print(f"database size: {size:.0f} KB\n")

    bench("read (SELECT)", lambda i: db.query(
        "SELECT COUNT(*) AS n FROM bench"), n)
    bench("write (INSERT)", lambda i: db.execute(
        "INSERT INTO bench (v) VALUES (?)", [f"row-{i}-{'x' * 100}"]), n)
    bench("read (lookup)", lambda i: db.query(
        "SELECT * FROM bench ORDER BY id DESC LIMIT 10"), n)

    print(f"\nserver view (last call): {db.last}")
    db.execute("DROP TABLE bench")  # clean up our own data
    print("cleaned up (DROP TABLE bench)")


if __name__ == "__main__":
    main()
