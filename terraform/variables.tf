# ---- deploy credentials ----
variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-2"
}

variable "aws_profile" {
  description = "Local AWS CLI profile to use; \"\" = default credential chain"
  type        = string
  default     = ""
}

variable "assume_role_arn" {
  description = "Deploy role to assume; \"\" = none. Pick this OR aws_profile."
  type        = string
  default     = ""

  validation {
    condition     = !(var.assume_role_arn != "" && var.aws_profile != "")
    error_message = "Set either aws_profile or assume_role_arn, not both."
  }
}

# ---- app ----
variable "env" {
  description = "Environment label used in tags. Put the env in db_bucket_name too (e.g. dev-srds-db-2026); inside the bucket the layout is always db/<db>.sqlite."
  type        = string
  default     = "dev"
}

variable "name" {
  description = "Project name; resources are <name>-db-lambda, <name>-demo-lambda, <name>-lambda-role"
  type        = string
  default     = "srds"
}

# ---- 数据库凭证(语义对齐 RDS 的 user/password,走 HTTP Basic) ----
variable "db_user" {
  description = "Database user, like the RDS master username"
  type        = string
}

variable "db_password" {
  description = "Database password. \"\" disables auth on the db lambda (public!)."
  type        = string
  sensitive   = true
}

# ---- demo 业务 API 自己的门(x-auth-token 头),与数据库凭证无关 ----
variable "auth_token" {
  description = "Shared secret for the demo service's own API gate"
  type        = string
  sensitive   = true
}
