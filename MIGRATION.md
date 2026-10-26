# Moving to RDS MySQL

s3rds is for the long tail: small databases, idle most of the time, ≈ $0. When you outgrow it, a real
MySQL is one import line away.

**When:** a database approaches tens of MB, writes arrive several times a second, you need an ORM,
sessions or row locks, or tools that speak the MySQL protocol (Workbench, DMS, Redshift federation).

1. Create the RDS MySQL 8.0 instance (db.t4g.micro is ≈ $12 / month in us-east-2 plus gp3 storage).
   The business Lambda must join the VPC (`vpc_config`, `AWSLambdaVPCAccessExecutionRole`) and its
   security group must be allowed on 3306; remove `reserved_concurrent_executions = 1` (that was the
   single-writer rule of SQLite, not of MySQL).
2. Copy the data: download the file and convert it.

   ```bash
   aws s3 cp s3://<bucket>/db/<name>.sqlite ./
   pip install sqlite3-to-mysql
   sqlite3mysql -f <name>.sqlite -d <name> -u <user> -p -h <rds-endpoint>    # schema + data
   ```

   Tens of MB take minutes. Cut-over: stop writes → migrate → `SELECT COUNT(*)` on both sides →
   switch the endpoint → apply.
3. Change the import: `from s3rds import pymysql` → `import pymysql`. `connect(host=, user=, password=,
   database=)` keeps the same shape, so the environment variables `DB_ENDPOINT / DB_NAME / DB_USER /
   DB_PASSWORD` just get RDS values. Add `pip install pymysql` to the Lambda package. What changes in
   the code: `commit()` now matters (`autocommit=False` is pymysql's default), `create_database()`
   and the cold-start "ensure table" go away (schema belongs to a migration script), and the SQLite-only
   bits (`STRICT`, `rowid`) are dropped — the compatibility report lists what already runs on both.
4. Move the password out of the var file into SSM Parameter Store, let RDS take the backups, and
   delete the stack: `terraform -chdir=terraform destroy -var-file=my.json`.

What you gain: sessions and transactions across requests, locks, ORMs, the MySQL protocol, unlimited
writers. What you lose: the ≈ $0 idle bill.
