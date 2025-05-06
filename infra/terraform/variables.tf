variable "project" {
  description = "Name prefix for every resource"
  type        = string
  default     = "sentinel"
}

variable "aws_region" {
  description = "Region to deploy into. Cost Explorer itself is global, but the function, logs and RDS are not."
  type        = string
  default     = "us-east-1"
}

variable "image_tag" {
  description = "ECR image tag to deploy. Immutable tags, so this is a real version, not 'latest'."
  type        = string

  validation {
    condition     = var.image_tag != "latest"
    error_message = "Use an immutable tag: 'latest' makes the deployed version unknowable."
  }
}

# --- Networking (assumed to exist) --------------------------------------

variable "private_subnet_ids" {
  description = "Private subnets with a route to RDS and a NAT route out for Cost Explorer and SES"
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups for the function's ENIs; must be allowed into the RDS security group"
  type        = list(string)
}

# --- Database -----------------------------------------------------------

variable "database_url" {
  description = "SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/sentinel"
  type        = string
  sensitive   = true
}

variable "database_secret_arn" {
  description = "Secrets Manager ARN holding the database credentials the function may read"
  type        = string
}

# --- Detection ----------------------------------------------------------

variable "zscore_threshold" {
  description = "Sigma threshold to flag a day"
  type        = number
  default     = 3.0

  validation {
    condition     = var.zscore_threshold > 0
    error_message = "zscore_threshold must be positive."
  }
}

variable "window_size" {
  description = "Trailing days in the baseline window"
  type        = number
  default     = 30
}

variable "poll_lookback_days" {
  description = "Days fetched per poll; must exceed window_size so a restated day is re-scored against a full baseline"
  type        = number
  default     = 45
}

# --- Alerting -----------------------------------------------------------

variable "alert_webhook_url" {
  description = "Webhook endpoint for alerts. Empty disables the channel."
  type        = string
  default     = ""
  sensitive   = true
}

variable "alert_email_from" {
  description = "Verified SES sender. The execution role's ses:SendRawEmail is conditioned on this address."
  type        = string
}

variable "alert_email_to" {
  description = "Alert recipients"
  type        = list(string)
}

variable "alert_cooldown_hours" {
  description = "Suppress repeat alerts for the same service inside this window"
  type        = number
  default     = 24
}

# --- Runtime ------------------------------------------------------------

variable "memory_size" {
  description = "Lambda memory in MB. Also sets CPU share, which matters because a cold start imports numpy and scikit-learn."
  type        = number
  default     = 1024

  validation {
    condition     = var.memory_size >= 1024
    error_message = "scikit-learn and numpy need at least 1024 MB to import without thrashing."
  }
}

variable "timeout_seconds" {
  description = "Invocation timeout. A full cycle walks poll_lookback_days of history for every service in the account."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds >= 60 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 60 and 900 (Lambda's maximum)."
  }
}

variable "schedule_expression" {
  description = "EventBridge schedule for the full poll cycle"
  type        = string
  default     = "rate(6 hours)"
}

variable "sweep_schedule_expression" {
  description = "EventBridge schedule for the alert retry sweep. More frequent than the poll and far cheaper."
  type        = string
  default     = "rate(1 hour)"
}

variable "stall_alarm_period_seconds" {
  description = "Window for the 'has not run' alarm. Longer than the poll interval so one skipped run does not page."
  type        = number
  default     = 43200 # 12h, against a 6h schedule
}

variable "log_retention_days" {
  description = "CloudWatch log retention. Lambda's implicit default is forever, at full price."
  type        = number
  default     = 30
}

variable "log_level" {
  description = "Application log level"
  type        = string
  default     = "INFO"
}

variable "alarm_sns_topic_arns" {
  description = "SNS topics notified by the CloudWatch alarms. Should not be the same channel Sentinel alerts into — if the function is down, that channel is exactly what is not working."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags applied to every resource"
  type        = map(string)
  default     = {}
}
