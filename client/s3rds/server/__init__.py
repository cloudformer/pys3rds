"""s3rds server: the Lambda behind the endpoint. pip install "s3rds[server]" (adds the pinned SQLite engine).

Lambda handler: s3rds.server.lambda_function.lambda_handler. Not imported here: lambda_function reads its
environment (DB_BUCKET, ...) at import time, and a client never needs it.
"""
