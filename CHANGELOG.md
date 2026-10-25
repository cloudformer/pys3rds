# Changelog

## 0.1.1 — 2026-10-25

`s3rds.sqlite` fixes (inherited by `s3rds.pymysql`). No breaking changes.

- **Parameters**: `datetime`, `date`, `time`, `Decimal`, `bool` and `None` are encoded before sending
  (`'YYYY-MM-DD HH:MM:SS[.ffffff]'`, `'YYYY-MM-DD'`, `'HH:MM:SS'`, decimal as string, bool as 1/0).
  `bytes` and unknown types raise `s3rds.sqlite.Error` naming the parameter, never a bare `TypeError`.
- **`executemany`**: `rowcount` is the sum of rows actually affected; `lastrowid` is the last statement's.
- **No silent truncation**: the server no longer cuts results at 500 rows. Results larger than 4 MB are paged
  (byte-based, under Lambda's 6 MB response limit) with a `next_token` that carries the database version;
  the driver fetches every page inside `execute()`, and if the database changes while paging the server
  refuses to mix versions and the driver re-runs the query (up to 3 times). A result inside `/tx` that
  exceeds a page is an explicit error. A new driver talking to a pre-0.1.1 server raises on `truncated`.
- **`rollback()`**: statements are durable as soon as they run, so `rollback()` cannot undo them; it now
  warns (`RuntimeWarning`) when something was written since the last `commit()`, and stays silent otherwise.

Server-side changes require a redeploy (`terraform apply` / `cloudformation deploy`); the new server still
serves 0.1.0 clients.

## 0.1.0 — 2026-10-25

First release: `s3rds.sqlite` and `s3rds.pymysql` drivers, web console, Terraform and CloudFormation deployments.
