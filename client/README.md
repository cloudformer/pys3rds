# pys3rds

**Python drivers for [pys3rds](https://github.com/cloudformer/pys3rds) — a truly serverless RDS:
a Lambda URL as your database endpoint, SQLite on S3, ≈ $0 when idle.**

Zero dependencies (standard library only). One install, drivers shaped like the standard clients,
so moving to a real RDS later is a one-line import change.

```bash
pip install pys3rds
```

```python
import pys3mysql                                  # later: import pymysql — nothing else changes

conn = pys3mysql.connect(
    host="xxxx.lambda-url.us-east-2.on.aws",
    user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())                         # [{'id': 1, 'title': 'hello'}]

conn.transaction([                                 # all-or-nothing, single S3 write
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

| Module | Mirrors | Status |
|---|---|---|
| `pys3mysql` | pymysql | available (SQLite dialect; MySQL dialect layer planned for 0.2) |
| `pys3pgsql` | psycopg2 | planned |

## Semantics

- Each `execute()` is its own transaction and is durable in S3 when it returns (`autocommit=True`).
  Use `transaction()` for multi-statement atomicity.
- Writes **never** duplicate: the driver re-sends only when the request provably never reached the
  server (stale keep-alive connection). Timeouts raise `Error` with an explicit "result unknown".
- Placeholders: `%s` (pymysql style) and `?` both work.

## Server

You need a pys3rds server in your own AWS account — deploy it once with Terraform or CloudFormation
from the [main repository](https://github.com/cloudformer/pys3rds). Same as any driver needs its database.
