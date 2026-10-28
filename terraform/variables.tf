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
  description = "Project name; resources are <name>-db-lambda, <name>-lambda-role"
  type        = string
  default     = "srds"
}

# ---- database credentials (RDS user / password semantics, sent as HTTP Basic) ----
variable "db_user" {
  description = "Database user, like the RDS master username"
  type        = string
}

variable "db_password" {
  description = "Database password. \"\" disables auth on the db lambda (public!)."
  type        = string
  sensitive   = true
}

# ---- the endpoint's gate ----
variable "endpoint_auth" {
  description = <<-EOT
    How the database endpoint (db / db-ro Function URLs) is protected:
      NONE    = public URL; only user/password (HTTP Basic) is checked      <- default
      AWS_IAM = AWS checks a SigV4 signature + IAM permission first, then user/password.
                Nobody without AWS credentials even reaches the lambda. The browser
                console cannot sign requests, so it is unusable in this mode.
  EOT
  type        = string
  default     = "NONE"

  validation {
    condition     = contains(["NONE", "AWS_IAM"], var.endpoint_auth)
    error_message = "endpoint_auth must be NONE or AWS_IAM."
  }
}

variable "console" {
  description = "Serve the built-in web console at GET / on the db endpoints. false = API only."
  type        = bool
  default     = true
}
