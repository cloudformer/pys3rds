data "aws_caller_identity" "me" {}

locals {
  prefix = "db/" # fixed layout in every environment: s3://<bucket>/db/<db>.sqlite
  role   = "${var.name}-lambda-role"
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

# ---------------- packaging: independent zips ----------------
data "archive_file" "db_code" {
  type        = "zip"
  source_dir  = "${path.module}/../src/db"
  excludes    = ["**/__pycache__/**", "**/.DS_Store"]
  output_path = "${path.module}/.build/db.zip"
}

# the demo zip = business code + the s3rds driver taken straight from client/ (the pip package source)
data "archive_file" "demo_code" {
  type        = "zip"
  output_path = "${path.module}/.build/demo.zip"
  source {
    filename = "lambda_function.py"
    content  = file("${path.module}/../src/demo/lambda_function.py")
  }
  dynamic "source" {
    for_each = fileset("${path.module}/../client/s3rds", "*.py")
    content {
      filename = "s3rds/${source.value}"
      content  = file("${path.module}/../client/s3rds/${source.value}")
    }
  }
}

# ---------------- Lambda 1: the database (DataGrip-style web console) ----------------
resource "aws_lambda_function" "db" {
  function_name    = "${var.name}-db-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
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
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
      CONSOLE     = var.console ? "1" : "0"
    }
  }
}

# ---------------- Lambda 1b: read-only endpoint (same code + READONLY=1, unlimited read concurrency) ----------------
# not a replica: it reads the same S3 file as the writer, zero copy, zero lag; just a second entrance.
resource "aws_lambda_function" "db_ro" {
  function_name    = "${var.name}-db-ro-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256
  # no reserved concurrency: reads do not conflict, scale with demand

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
      READONLY    = "1"
      CONSOLE     = var.console ? "1" : "0"
    }
  }
}

resource "aws_lambda_function_url" "db_ro" {
  function_name      = aws_lambda_function.db_ro.function_name
  authorization_type = var.endpoint_auth
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_ro_url" {
  count                  = var.endpoint_auth == "NONE" ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db_ro.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_ro_url_invoke" {
  count                    = var.endpoint_auth == "NONE" ? 1 : 0
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db_ro.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------- Lambda 2: the demo service (uses the database endpoint like RDS) ----------------
resource "aws_lambda_function" "demo" {
  function_name    = "${var.name}-demo-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.demo_code.output_path
  source_code_hash = data.archive_file.demo_code.output_base64sha256
  timeout          = 15
  memory_size      = 256

  reserved_concurrent_executions = 1

  environment {
    variables = {
      # the same parameters as RDS: endpoint / dbname / user / password.
      # moving to a real RDS: point these four at it, swap the driver, the business code stays.
      DB_ENDPOINT = aws_lambda_function_url.db.function_url
      DB_NAME     = "demo"
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
      AUTH_TOKEN  = var.auth_token # the demo API's own gate, unrelated to the database credentials
    }
  }
}

# ---------------- Function URLs ----------------
# endpoint_auth = NONE:    public URL, our user/password is the only gate. A public URL
#                          needs BOTH permission statements (AWS requirement since Oct 2025):
#                            lambda:InvokeFunctionUrl  with FunctionUrlAuthType = NONE
#                            lambda:InvokeFunction     with InvokedViaFunctionUrl = true
# endpoint_auth = AWS_IAM: AWS verifies SigV4 + IAM first; no public permissions are created.
resource "aws_lambda_function_url" "db" {
  function_name      = aws_lambda_function.db.function_name
  authorization_type = var.endpoint_auth
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_url" {
  count                  = var.endpoint_auth == "NONE" ? 1 : 0
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_url_invoke" {
  count                    = var.endpoint_auth == "NONE" ? 1 : 0
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db.function_name
  principal                = "*"
  invoked_via_function_url = true
}

resource "aws_lambda_function_url" "demo" {
  function_name      = aws_lambda_function.demo.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "x-auth-token"]
  }
}

resource "aws_lambda_permission" "demo_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.demo.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "demo_url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.demo.function_name
  principal                = "*"
  invoked_via_function_url = true
}
