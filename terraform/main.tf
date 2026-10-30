data "aws_caller_identity" "me" {}

locals {
  prefix = "db/" # fixed layout in every environment: s3://<bucket>/db/<db>.sqlite
  role   = "${var.project_name}-lambda-role"
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

# ---------------- IAM: one role for the lambdas, scoped to db/*.sqlite ----------------
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
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
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

# ---------------- Lambda 1: the database (DataGrip-style web console) ----------------
resource "aws_lambda_function" "db" {
  function_name    = "${var.project_name}-db-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "s3rds.server.lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256

  # one writer globally: no concurrent writes by construction (the ETag CAS in the code is the second guard)
  reserved_concurrent_executions = 1

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.console_auth_username
      DB_PASSWORD = var.console_auth_password
      CONSOLE     = var.console ? "1" : "0"
    }
  }
}

# TODO: review — Lambda 1 and 1b differ only in READONLY and the reserved concurrency: one for_each over {rw, ro}.
# ---------------- Lambda 1b: read-only endpoint (same code + READONLY=1, unlimited read concurrency) ----------------
# not a replica: it reads the same S3 file as the writer, zero copy, zero lag; just a second entrance.
resource "aws_lambda_function" "db_ro" {
  function_name    = "${var.project_name}-db-ro-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "s3rds.server.lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256
  # no reserved concurrency: reads do not conflict, scale with demand

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.console_auth_username
      DB_PASSWORD = var.console_auth_password
      READONLY    = "1"
      CONSOLE     = var.console ? "1" : "0"
    }
  }
}

resource "aws_lambda_function_url" "db_ro" {
  function_name      = aws_lambda_function.db_ro.function_name
  authorization_type = var.function_url_auth
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_ro_url" {
  count                  = var.function_url_auth == "NONE" ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db_ro.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_ro_url_invoke" {
  count                    = var.function_url_auth == "NONE" ? 1 : 0
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db_ro.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------- Function URLs ----------------
# function_url_auth = NONE:    public URL, our user/password is the only gate. A public URL
#                              needs BOTH permission statements (AWS requirement since Oct 2025):
#                            lambda:InvokeFunctionUrl  with FunctionUrlAuthType = NONE
#                            lambda:InvokeFunction     with InvokedViaFunctionUrl = true
# function_url_auth = AWS_IAM: AWS verifies SigV4 + IAM first; no public permissions are created.
resource "aws_lambda_function_url" "db" {
  function_name      = aws_lambda_function.db.function_name
  authorization_type = var.function_url_auth
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_url" {
  count                  = var.function_url_auth == "NONE" ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_url_invoke" {
  count                    = var.function_url_auth == "NONE" ? 1 : 0
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db.function_name
  principal                = "*"
  invoked_via_function_url = true
}
