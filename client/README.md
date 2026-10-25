# s3rds

**Python drivers for [s3rds](https://github.com/cloudformer/s3rds) — a truly serverless RDS:
a Lambda URL as your database endpoint, SQLite on S3, ≈ $0 when idle.**

![s3rds architecture](https://raw.githubusercontent.com/cloudformer/s3rds/main/docs/architecture.svg)

Zero dependencies (standard library only). Each driver is a submodule **named after the standard
client it mirrors**, so moving to a real database is a one-line import change and nothing else:

```bash
pip install s3rds                # s3rds.sqlite: SQLite dialect
pip install "s3rds[mysql]"       # + s3rds.pymysql: write MySQL, translated on the client (sqlglot)
```

```python
from s3rds import pymysql        # today   -- later: `import pymysql` on a real RDS, rest unchanged

conn = pymysql.connect(
    host="xxxx.lambda-url.us-east-2.on.aws",
    user="admin", password="...", database="blog")

with conn.cursor() as cur:
    cur.execute("CREATE TABLE posts (id INT AUTO_INCREMENT PRIMARY KEY, title VARCHAR(200))")
    cur.execute("INSERT INTO posts (title) VALUES (%s)", ["hello"])
    print(cur.lastrowid)

with conn.cursor(pymysql.cursors.DictCursor) as cur:
    cur.execute("SELECT * FROM posts WHERE id = %s", [1])
    print(cur.fetchall())                         # [{'id': 1, 'title': 'hello'}]

conn.transaction([                                 # all-or-nothing, single S3 write
    ("UPDATE stock SET qty = qty - 1 WHERE id = %s", [7]),
    ("INSERT INTO orders (item) VALUES (%s)", [7]),
])
```

| Submodule | SQL dialect | Mirrors | Status |
|---|---|---|---|
| `s3rds.sqlite` | SQLite (no translation) | pymysql-shaped API | available |
| `s3rds.pymysql` | MySQL, translated on the client | pymysql | available — `pip install "s3rds[mysql]"` |
| `s3rds.psycopg2` | PostgreSQL | psycopg2 | placeholder; `connect()` raises until implemented |

## Semantics

- Each `execute()` is its own transaction and is durable in S3 when it returns (`autocommit=True`).
  Use `transaction()` for multi-statement atomicity.
- Writes **never** duplicate: the driver re-sends only when the request provably never reached the
  server (stale keep-alive connection). Timeouts raise an error with an explicit "result unknown".
- `s3rds.pymysql` raises pymysql-style exceptions with MySQL error codes (1062, 1146, 1054, 1064, …);
  unsupported MySQL features raise `NotSupportedError` instead of misbehaving silently.

## Server

You need an s3rds server in your own AWS account — deploy it once with Terraform or CloudFormation
from the [main repository](https://github.com/cloudformer/s3rds). Same as any driver needs its database.
