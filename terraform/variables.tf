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
  description = "Project name; resources are <project_name>-db-lambda, <project_name>-db-ro-lambda, <project_name>-rw-lambda-role, <project_name>-ro-lambda-role"
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
  description = "Database password (HTTP Basic): console login AND the password= clients connect() with. At least 16 characters."
  type        = string
  sensitive   = true

  validation {
    condition     = length(var.console_auth_password) >= 16
    error_message = "console_auth_password must be at least 16 characters (the endpoint is a public URL)."
  }
}

variable "db_ro" {
  description = "Also deploy the read-only endpoint (<project_name>-db-ro-lambda): its own role, GetObject only, unlimited concurrency. false = the read/write endpoint only."
  type        = bool
  default     = true
}

# ---- data ----
variable "force_destroy" {
  description = "true = terraform destroy deletes the bucket WITH every database in it. false = destroy stops at a non-empty bucket and the data survives."
  type        = bool
  default     = true
}
