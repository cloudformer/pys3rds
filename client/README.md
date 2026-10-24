# pys3mysql

**MySQL-driver-shaped interface · SQLite engine · S3 storage · Lambda compute.**
Python client for [srds](https://github.com/frankzhang/serverless-rds-s3), a serverless SQL
database that costs ≈ $0 when idle (S3 storage only; Lambda stays inside the permanent free tier).
Zero dependencies (standard library only).

The API mirrors `pymysql`, so moving to a real RDS MySQL later is a one-line change:
`import pys3mysql` → `import pymysql`.

```python
import pys3mysql

conn = pys3mysql.connect(
    host="xxxx.lambda-url.us-east-2.on.aws",
    user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(pys3mysql.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())        # [{'id': 1, 'title': 'hello'}]

# Multi-statement atomic transaction (all-or-nothing, single S3 write)
conn.transaction([
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

## Semantics

- Each `execute()` is its own transaction and is durable in S3 when it returns
  (equivalent to `autocommit=True`). Use `transaction()` for multi-statement atomicity.
- Writes **never** duplicate: the client re-sends only when the request provably never
  reached the server (stale keep-alive connection). Timeouts raise `Error` with an
  explicit "result unknown" message.
- SQL dialect is SQLite (`STRICT` tables, `AUTOINCREMENT`, `?` or `%s` placeholders).

## Server

The server side (Lambda + S3 + web console + Terraform) lives in the main repo.
