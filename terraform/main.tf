data "aws_caller_identity" "me" {}

locals {
  prefix = "db/" # fixed layout in every environment: s3://<bucket>/db/<db>.sqlite
  role   = "${var.project_name}-lambda-role"

  # the two endpoints run the same code:
  #   rw  the database, one writer globally (reserved concurrency 1; the ETag CAS in the code is the second guard)
  #   ro  read-only (READONLY=1): not a replica, it reads the same S3 file, zero copy, zero lag; scales with demand
  endpoints = {
    rw = { function_name = "${var.project_name}-db-lambda", readonly = "0", concurrency = 1 }
    ro = { function_name = "${var.project_name}-db-ro-lambda", readonly = "1", concurrency = -1 }
  }
}

# ---------------- S3: every database file lives in this one private bucket ----------------
resource "aws_s3_bucket" "db" {
  # S3 bucket names are global; change the suffix if this one is taken
  bucket        = "${var.env}-srds-db-2026"
  force_destroy = true # destroy deletes the data too; copy out anything you want to keep first
}

resource "aws_s3_bucket_public_access_block" "db" {
  bucket                  = aws_s3_bucket.db.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "db" {
  bucket = aws_s3_bucket.db.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" } # SSE-S3, no KMS cost
  }
}

# TLS only: any request over plain HTTP is denied. Principal "*" here means "whoever asks" in a Deny,
# so it grants nothing; the public access block above stays satisfied.
resource "aws_s3_bucket_policy" "db" {
  bucket = aws_s3_bucket.db.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.db.arn, "${aws_s3_bucket.db.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.db]
}

# ---------------- logs: one group per Lambda, kept 14 days ----------------
resource "aws_cloudwatch_log_group" "db" {
  for_each          = local.endpoints
  name              = "/aws/lambda/${each.value.function_name}"
  retention_in_days = 14
}

# ---------------- IAM: one role, scoped to db/*.sqlite and the two log groups ----------------
resource "aws_iam_role" "lambda" {
  name = local.role
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.role}-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for g in aws_cloudwatch_log_group.db : "${g.arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.db.arn}/db/*.sqlite"
      },
      {
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = aws_s3_bucket.db.arn
        Condition = { StringLike = { "s3:prefix" = ["db/*"] } }
      }
    ]
  })
}

# ---------------- packaging: the pip package s3rds[server] (s3rds.server + the pinned engine, Linux wheels) ----------------
# scripts/build_server.py stages terraform/.build/db at plan time
data "external" "db_build" {
  program = ["python3", "${path.module}/../scripts/build_server.py", var.server_package]
}

data "archive_file" "db_code" {
  type        = "zip"
  source_dir  = data.external.db_build.result.dir
  output_path = "${path.module}/.build/db.zip"
}

# ---------------- Lambda: the database (web console + API), read/write and read-only ----------------
resource "aws_lambda_function" "db" {
  for_each         = local.endpoints
  function_name    = each.value.function_name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "s3rds.server.lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256

  reserved_concurrent_executions = each.value.concurrency

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.console_auth_username
      DB_PASSWORD = var.console_auth_password
      READONLY    = each.value.readonly
      CONSOLE     = var.console ? "1" : "0"
    }
  }

  depends_on = [aws_cloudwatch_log_group.db]
}

# ---------------- Function URLs ----------------
# No CORS: the console is served by the same URL (same origin) and the drivers are not browsers.
resource "aws_lambda_function_url" "db" {
  for_each           = local.endpoints
  function_name      = aws_lambda_function.db[each.key].function_name
  authorization_type = var.function_url_auth
}

# function_url_auth = NONE: a public URL. AWS requires a resource policy that lets anyone invoke it, so the two
# statements below say principal = "*" — that is what "public URL" means, not an open database: every request
# still has to pass HTTP Basic (console_auth_username / console_auth_password) inside the Lambda, or it gets 401.
# Since Oct 2025 AWS requires BOTH statements:
#   lambda:InvokeFunctionUrl  with FunctionUrlAuthType = NONE
#   lambda:InvokeFunction     with InvokedViaFunctionUrl = true (only through the URL, not a direct Invoke)
# function_url_auth = AWS_IAM: AWS verifies SigV4 + IAM first; neither statement is created.
resource "aws_lambda_permission" "url" {
  for_each               = var.function_url_auth == "NONE" ? local.endpoints : {}
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db[each.key].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  for_each                 = var.function_url_auth == "NONE" ? local.endpoints : {}
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db[each.key].function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------- renamed in 0.3.3 (rw / ro became one for_each): keep the deployed resources, URLs unchanged ----------------
moved {
  from = aws_lambda_function.db
  to   = aws_lambda_function.db["rw"]
}
moved {
  from = aws_lambda_function.db_ro
  to   = aws_lambda_function.db["ro"]
}
moved {
  from = aws_lambda_function_url.db
  to   = aws_lambda_function_url.db["rw"]
}
moved {
  from = aws_lambda_function_url.db_ro
  to   = aws_lambda_function_url.db["ro"]
}
moved {
  from = aws_lambda_permission.db_url[0]
  to   = aws_lambda_permission.url["rw"]
}
moved {
  from = aws_lambda_permission.db_ro_url[0]
  to   = aws_lambda_permission.url["ro"]
}
moved {
  from = aws_lambda_permission.db_url_invoke[0]
  to   = aws_lambda_permission.url_invoke["rw"]
}
moved {
  from = aws_lambda_permission.db_ro_url_invoke[0]
  to   = aws_lambda_permission.url_invoke["ro"]
}
