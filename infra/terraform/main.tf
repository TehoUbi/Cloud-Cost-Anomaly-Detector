# Sentinel poller on Lambda, scheduled by EventBridge.
#
# Scope: the scheduled detector only. The VPC, subnets, security groups and
# the RDS instance are assumed to exist and are passed in as variables —
# a cost monitor should not own the networking of the account it watches.
#
#   terraform init
#   terraform apply -var-file=prod.tfvars
#
# The image must be in ECR first:
#   aws ecr get-login-password --region "$REGION" \
#     | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
#   docker build -f infra/Dockerfile -t sentinel-poller .
#   docker tag sentinel-poller:latest "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:$TAG"
#   docker push "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/sentinel-poller:$TAG"

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

locals {
  name = "${var.project}-poller"
  tags = merge(var.tags, {
    Project   = var.project
    ManagedBy = "terraform"
  })
}

# --- Image repository ----------------------------------------------------

resource "aws_ecr_repository" "poller" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "poller" {
  repository = aws_ecr_repository.poller.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# --- Execution role ------------------------------------------------------

resource "aws_iam_role" "poller" {
  name = "${local.name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = local.tags
}

# Required for a Lambda in a VPC to create and tear down its ENIs.
resource "aws_iam_role_policy_attachment" "vpc_access" {
  role       = aws_iam_role.poller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "poller" {
  # Cost Explorer takes no resource ARNs — ce:* is account-wide by design, so
  # "*" here is the only valid form, not a shortcut.
  statement {
    sid       = "ReadCostExplorer"
    effect    = "Allow"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }

  statement {
    sid    = "SendAlertEmail"
    effect = "Allow"
    actions = [
      "ses:SendRawEmail",
      "ses:SendEmail",
    ]
    resources = ["*"]
    # Narrowed to the one verified identity rather than any identity the
    # account owns.
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [var.alert_email_from]
    }
  }

  statement {
    sid    = "ReadDatabaseCredentials"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [var.database_secret_arn]
  }

  statement {
    sid    = "WriteLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.poller.arn}:*"]
  }
}

resource "aws_iam_role_policy" "poller" {
  name   = "${local.name}-policy"
  role   = aws_iam_role.poller.id
  policy = data.aws_iam_policy_document.poller.json
}

# --- Function ------------------------------------------------------------

# Created here rather than letting Lambda create it implicitly, so the
# retention period is set and logs are not kept forever at full price.
resource "aws_cloudwatch_log_group" "poller" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
  tags              = local.tags
}

resource "aws_lambda_function" "poller" {
  function_name = local.name
  role          = aws_iam_role.poller.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.poller.repository_url}:${var.image_tag}"

  # A cold start imports numpy and scikit-learn and the cycle then walks 45
  # days of history for every service in the account. 512 MB is not enough for
  # the former and the default 3s timeout is not enough for the latter.
  memory_size = var.memory_size
  timeout     = var.timeout_seconds

  # Lambda allocates CPU in proportion to memory, so the fit here is about
  # import time as much as footprint.
  architectures = ["x86_64"]

  environment {
    variables = {
      DATABASE_URL           = var.database_url
      COST_PROVIDER          = "aws"
      AWS_DEFAULT_REGION     = var.aws_region
      ZSCORE_THRESHOLD       = tostring(var.zscore_threshold)
      WINDOW_SIZE            = tostring(var.window_size)
      POLL_LOOKBACK_DAYS     = tostring(var.poll_lookback_days)
      ALERT_WEBHOOK_URL      = var.alert_webhook_url
      ALERT_EMAIL_TRANSPORT  = "ses"
      ALERT_EMAIL_FROM       = var.alert_email_from
      ALERT_EMAIL_TO         = join(",", var.alert_email_to)
      ALERT_COOLDOWN_HOURS   = tostring(var.alert_cooldown_hours)
      LOG_LEVEL              = var.log_level
      SENTINEL_WARM_IMPORTS  = "1"
      # One connection per invocation: the function is not concurrent, and a
      # large pool behind a frozen container is just idle RDS connections.
      DB_POOL_SIZE           = "2"
      DB_MAX_OVERFLOW        = "2"
    }
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = var.security_group_ids
  }

  # Two concurrent polls would both read the same window and race on the
  # upsert. The schedule is every few hours; one at a time is the correct
  # amount.
  reserved_concurrent_executions = 1

  depends_on = [
    aws_iam_role_policy.poller,
    aws_cloudwatch_log_group.poller,
  ]

  tags = local.tags
}

# --- Schedule ------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "poll" {
  name                = "${local.name}-schedule"
  description         = "Run a Sentinel poll cycle"
  schedule_expression = var.schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "poll" {
  rule      = aws_cloudwatch_event_rule.poll.name
  target_arn = aws_lambda_function.poller.arn
  input     = jsonencode({ action = "poll" })

  retry_policy {
    # The handler returns a 500 payload rather than raising, so EventBridge
    # does not normally retry. This covers invocation-level failure only.
    maximum_retry_attempts       = 2
    maximum_event_age_in_seconds = 3600
  }
}

# A second, more frequent rule that only flushes the alert retry backlog.
# Cheap, and it means a webhook outage is cleared within the hour instead of
# waiting for the next full poll.
resource "aws_cloudwatch_event_rule" "sweep" {
  name                = "${local.name}-sweep"
  description         = "Retry alerts whose backoff has elapsed"
  schedule_expression = var.sweep_schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "sweep" {
  rule       = aws_cloudwatch_event_rule.sweep.name
  target_arn = aws_lambda_function.poller.arn
  input      = jsonencode({ action = "sweep" })
}

resource "aws_lambda_permission" "poll" {
  statement_id  = "AllowExecutionFromEventBridgePoll"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.poll.arn
}

resource "aws_lambda_permission" "sweep" {
  statement_id  = "AllowExecutionFromEventBridgeSweep"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweep.arn
}

# --- Alarms --------------------------------------------------------------
# The monitor needs monitoring: a detector that silently stops running looks
# exactly like an account with no cost anomalies.

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${local.name}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Sentinel poller invocation failed"
  dimensions          = { FunctionName = aws_lambda_function.poller.function_name }
  alarm_actions       = var.alarm_sns_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "not_running" {
  alarm_name          = "${local.name}-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  # Longer than the poll interval, so a single skipped run does not alarm.
  period              = var.stall_alarm_period_seconds
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Sentinel poller has not run — cost anomalies are going undetected"
  dimensions          = { FunctionName = aws_lambda_function.poller.function_name }
  alarm_actions       = var.alarm_sns_topic_arns
  # Missing data is the failure mode being watched for here.
  treat_missing_data  = "breaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "duration" {
  alarm_name          = "${local.name}-duration"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Maximum"
  # Fires before the function starts timing out, not after.
  threshold           = var.timeout_seconds * 1000 * 0.8
  alarm_description   = "Sentinel poller is approaching its timeout"
  dimensions          = { FunctionName = aws_lambda_function.poller.function_name }
  alarm_actions       = var.alarm_sns_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}
