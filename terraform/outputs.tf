output "db_url" {
  description = "The database endpoint; open it in a browser for the DataGrip-style console"
  value       = aws_lambda_function_url.db["rw"].function_url
}

output "db_ro_url" {
  description = "Read-only endpoint: unlimited concurrency for read-heavy code (writes get 403); null with db_ro = false"
  value       = var.db_ro ? aws_lambda_function_url.db["ro"].function_url : null
}

output "db_bucket" {
  value = aws_s3_bucket.db.bucket
}

output "db_prefix" {
  description = "Database file prefix: s3://<bucket>/<prefix><db>.sqlite"
  value       = local.prefix
}
