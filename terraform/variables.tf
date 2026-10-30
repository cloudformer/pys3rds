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
  description = "Environment label used in tags."
  type        = string
  default     = "dev"
}

variable "project_name" {
  description = "Project name; resources are <project_name>-db-lambda, <project_name>-lambda-role"
  type        = string
  default     = "srds"
}

# ---- the Function URLs (db / db-ro): console, gate, login ----
variable "console" {
  description = "Serve the built-in web console at GET / on the db endpoints. false = API only."
  type        = bool
  default     = true
}

variable "function_url_auth" {
  description = <<-EOT
    How the database endpoint (db / db-ro Function URLs) is protected:
      NONE    = public URL; only console_auth_username/password (HTTP Basic) is checked      <- default
      AWS_IAM = AWS checks a SigV4 signature + IAM permission first, then user/password.
                Nobody without AWS credentials even reaches the lambda. The browser
                console cannot sign requests, so it is unusable in this mode.
  EOT
  type        = string
  default     = "NONE"

  validation {
    condition     = contains(["NONE", "AWS_IAM"], var.function_url_auth)
    error_message = "function_url_auth must be NONE or AWS_IAM."
  }
}

variable "console_auth_username" {
  description = "Database user (HTTP Basic): console login AND the user= clients connect() with, like the RDS master username; still required with console = false"
  type        = string
}

variable "console_auth_password" {
  description = "Database password (HTTP Basic): console login AND the password= clients connect() with. \"\" disables auth on the db lambda (public!)."
  type        = string
  sensitive   = true
}

# ---- s3rds ----
variable "server_package" {
  description = "What the db lambdas run, as pip installs it: a release (\"s3rds[server]==0.3.1\") or this working tree (\"./client[server]\", relative to the repo root; before a release)"
  type        = string
}
