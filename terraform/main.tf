data "aws_caller_identity" "me" {}

locals {
  prefix = "db/" # 桶内目录固定,所有环境一致:s3://<bucket>/db/<db>.sqlite
  role   = "${var.name}-lambda-role"
}

# ---------------- S3:数据库文件都在这一个私有 bucket ----------------
resource "aws_s3_bucket" "db" {
  # S3 桶名全局唯一,被占了改这里的后缀
  bucket        = "${var.env}-srds-db-2026"
  force_destroy = true # 自己玩:destroy 连数据一起删;有要留的数据先 aws s3 cp 备份
}

resource "aws_s3_bucket_public_access_block" "db" {
  bucket                  = aws_s3_bucket.db.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------- IAM:两个 lambda 共用一个 role,只放行 <env>/*.sqlite ----------------
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

# ---------------- 打包:两个 lambda 完全独立,各自一个 zip ----------------
data "archive_file" "db_code" {
  type        = "zip"
  source_dir  = "${path.module}/../src/db"
  excludes    = ["**/__pycache__/**", "**/.DS_Store"]
  output_path = "${path.module}/.build/db.zip"
}

# demo 的 zip = 业务代码 + 驱动;驱动直接取 client/ 下那一份(pip 包的源码),不另拷
data "archive_file" "demo_code" {
  type        = "zip"
  output_path = "${path.module}/.build/demo.zip"
  source {
    filename = "lambda_function.py"
    content  = file("${path.module}/../src/demo/lambda_function.py")
  }
  source {
    filename = "pys3mysql.py"
    content  = file("${path.module}/../client/pys3mysql.py")
  }
}

# ---------------- Lambda 1:通用 serverless 数据库(DataGrip 风格 web 控制台) ----------------
resource "aws_lambda_function" "db" {
  function_name    = "${var.name}-db-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256

  # 全局单写者:物理上杜绝并发写冲突(代码里的 ETag CAS 是第二道保险)
  reserved_concurrent_executions = 1

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
    }
  }
}

# ---------------- Lambda 1b:只读入口(同一份代码+READONLY=1,读不限并发) ----------------
# 不是"副本":和写者读同一份 S3 文件,零复制零延迟,只是第二个入口。
resource "aws_lambda_function" "db_ro" {
  function_name    = "${var.name}-db-ro-lambda"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.db_code.output_path
  source_code_hash = data.archive_file.db_code.output_base64sha256
  timeout          = 15
  memory_size      = 256
  # 不设 reserved_concurrency:读互不冲突,随并发横向扩

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
      READONLY    = "1"
    }
  }
}

resource "aws_lambda_function_url" "db_ro" {
  function_name      = aws_lambda_function.db_ro.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_ro_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db_ro.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_ro_url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db_ro.function_name
  principal                = "*"
  invoked_via_function_url = true
}

# ---------------- Lambda 2:业务 lambda 示例(把 db lambda 当 RDS endpoint 用) ----------------
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
      # 连接参数与 RDS 完全同构:endpoint / dbname / user / password。
      # 未来迁真 RDS:这四个换成 RDS 的值,业务代码不动,只换驱动。
      DB_ENDPOINT = aws_lambda_function_url.db.function_url
      DB_NAME     = "demo"
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
      AUTH_TOKEN  = var.auth_token # demo 自己 API 的门,与数据库凭证无关
    }
  }
}

# ---------------- Function URLs(公网,靠 x-auth-token 挡门) ----------------
# Public URL needs BOTH statements (AWS requirement since Oct 2025):
#   lambda:InvokeFunctionUrl  with FunctionUrlAuthType = NONE
#   lambda:InvokeFunction     with InvokedViaFunctionUrl = true
resource "aws_lambda_function_url" "db" {
  function_name      = aws_lambda_function.db.function_name
  authorization_type = "NONE"
  cors {
    allow_origins = ["*"]
    allow_methods = ["*"]
    allow_headers = ["content-type", "authorization"]
  }
}

resource "aws_lambda_permission" "db_url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "db_url_invoke" {
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
