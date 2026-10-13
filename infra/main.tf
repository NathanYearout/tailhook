terraform {
  required_version = ">= 1.10"

  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 6.68" }
    archive = { source = "hashicorp/archive", version = "~> 2.8" }
    random  = { source = "hashicorp/random", version = "~> 3.9" }
  }
}

provider "aws" {
  region = "us-east-2"

  default_tags {
    tags = { project = "tailhook" }
  }
}

variable "alert_email" {
  type        = string
  description = "Where alarms and budget warnings are sent."
}

variable "llm_provider" {
  type        = string
  default     = "bedrock"
  description = "bedrock, or anthropic to call the Claude API with the key stored in SSM."

  validation {
    condition     = contains(["bedrock", "anthropic"], var.llm_provider)
    error_message = "llm_provider must be bedrock or anthropic."
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  # A separate workspace (e.g. "load") gets its own copy of everything.
  name    = terraform.workspace == "default" ? "tailhook" : "tailhook-${terraform.workspace}"
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
  models  = ["claude-haiku-5-5", "claude-opus-5-5"]

  # Tight on the public demo to cap what a flood can cost; a load test needs ~2,000 req/s.
  throttle = terraform.workspace == "default" ? { rate = 20, burst = 40 } : { rate = 2000, burst = 2000 }

  inbound_secret  = "whsec_${random_bytes.inbound_secret.base64}"
  outbound_secret = "whsec_${random_bytes.outbound_secret.base64}"

  base_url = aws_apigatewayv2_api.http.api_endpoint
}

resource "random_bytes" "inbound_secret" {
  length = 32
}

resource "random_bytes" "outbound_secret" {
  length = 32
}

# --- storage ---------------------------------------------------------------

resource "aws_dynamodb_table" "events" {
  name         = "${local.name}-events"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }
  attribute {
    name = "payment_id"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "S"
  }
  attribute {
    name = "shard"
    type = "S"
  }
  attribute {
    name = "received_at"
    type = "S"
  }

  global_secondary_index {
    name            = "by_payment"
    projection_type = "ALL"

    key_schema {
      attribute_name = "payment_id"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
  }

  global_secondary_index {
    name            = "feed"
    projection_type = "ALL"

    key_schema {
      attribute_name = "shard"
      key_type       = "HASH"
    }
    key_schema {
      attribute_name = "received_at"
      key_type       = "RANGE"
    }
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

resource "aws_sqs_queue" "dead" {
  name                      = "${local.name}-dead"
  message_retention_seconds = 1209600
  max_message_size          = 1048576 # a 200 KB body can reach 400 KB once escaped
}

resource "aws_sqs_queue" "events" {
  name                       = "${local.name}-events"
  visibility_timeout_seconds = 360
  message_retention_seconds  = 345600
  max_message_size           = 1048576

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dead.arn
    maxReceiveCount     = 3
  })
}

# --- functions -------------------------------------------------------------

data "archive_file" "core" {
  type        = "zip"
  source_dir  = "${path.module}/../build/core"
  output_path = "${path.module}/../build/core.zip"
}

data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${path.module}/../build/api"
  output_path = "${path.module}/../build/api.zip"
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ingest" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.events.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.events.arn]
  }
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.events.arn]
  }
}

data "aws_iam_policy_document" "api" {
  statement {
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.events.arn, "${aws_dynamodb_table.events.arn}/index/*"]
  }
  statement {
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
      "sqs:ChangeMessageVisibility",
    ]
    resources = [aws_sqs_queue.dead.arn]
  }
  statement {
    actions = ["bedrock:InvokeModel"]
    resources = concat(
      [for m in local.models : "arn:aws:bedrock:${local.region}:${local.account}:inference-profile/us.anthropic.${m}"],
      [for m in local.models : "arn:aws:bedrock:*::foundation-model/anthropic.${m}"],
    )
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${local.region}:${local.account}:parameter/tailhook/anthropic-api-key"]
  }
}

locals {
  policies = {
    ingest = data.aws_iam_policy_document.ingest.json
    worker = data.aws_iam_policy_document.worker.json
    api    = data.aws_iam_policy_document.api.json
  }
}

resource "aws_iam_role" "fn" {
  for_each           = local.policies
  name               = "${local.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "fn" {
  for_each = local.policies
  role     = aws_iam_role.fn[each.key].id
  policy   = each.value
}

resource "aws_iam_role_policy_attachment" "logs" {
  for_each   = local.policies
  role       = aws_iam_role.fn[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.policies
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = 14
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${local.name}-ingest"
  role             = aws_iam_role.fn["ingest"].arn
  runtime          = "python3.14"
  architectures    = ["arm64"]
  handler          = "app.ingest.handler"
  filename         = data.archive_file.core.output_path
  source_code_hash = data.archive_file.core.output_base64sha256
  timeout          = 5
  memory_size      = 256

  environment {
    variables = {
      QUEUE_URL      = aws_sqs_queue.events.url
      INBOUND_SECRET = local.inbound_secret
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "worker" {
  function_name    = "${local.name}-worker"
  role             = aws_iam_role.fn["worker"].arn
  runtime          = "python3.14"
  architectures    = ["arm64"]
  handler          = "app.worker.handler"
  filename         = data.archive_file.core.output_path
  source_code_hash = data.archive_file.core.output_base64sha256
  timeout          = 60
  memory_size      = 256

  environment {
    variables = {
      TABLE_NAME      = aws_dynamodb_table.events.name
      QUEUE_URL       = aws_sqs_queue.events.url
      DESTINATION_URL = "${local.base_url}/demo/merchant"
      OUTBOUND_SECRET = local.outbound_secret
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  role             = aws_iam_role.fn["api"].arn
  runtime          = "python3.14"
  architectures    = ["arm64"]
  handler          = "app.api.handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  timeout          = 29
  memory_size      = 512

  environment {
    variables = {
      TABLE_NAME      = aws_dynamodb_table.events.name
      DLQ_URL         = aws_sqs_queue.dead.url
      WEBHOOK_URL     = "${local.base_url}/webhooks"
      DESTINATION_URL = "${local.base_url}/demo/merchant"
      INBOUND_SECRET  = local.inbound_secret
      OUTBOUND_SECRET = local.outbound_secret
      LLM_PROVIDER    = var.llm_provider
    }
  }

  depends_on = [aws_cloudwatch_log_group.fn]
}

resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn        = aws_sqs_queue.events.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 5 # a delivery can take 10 s (connect, then read) and the worker has 60
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 100
  }
}

# --- HTTP API --------------------------------------------------------------

locals {
  api_routes = [
    "GET /",
    "GET /api/events",
    "GET /api/events/{id}",
    "GET /api/payments/{id}",
    "POST /api/payments/{id}/replay",
    "POST /api/dead-letters/retry",
    "GET /api/stats",
    "POST /api/simulate",
    "POST /api/merchant",
    "POST /api/ask",
    "POST /demo/merchant",
  ]

  # Anyone can press these on the public demo, so they get a tight limit.
  throttled_routes = [
    "POST /api/ask",
    "POST /api/simulate",
    "POST /api/payments/{id}/replay",
    "POST /api/dead-letters/retry",
    "POST /api/merchant",
  ]
}

resource "aws_apigatewayv2_api" "http" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "ingest" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingest.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}

resource "aws_apigatewayv2_route" "ingest" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /webhooks"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "api" {
  for_each  = toset(local.api_routes)
  api_id    = aws_apigatewayv2_api.http.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = local.throttle.burst
    throttling_rate_limit  = local.throttle.rate
  }

  dynamic "route_settings" {
    for_each = toset(local.throttled_routes)

    content {
      route_key              = route_settings.value
      throttling_burst_limit = 3
      throttling_rate_limit  = 1
    }
  }

  depends_on = [aws_apigatewayv2_route.api]
}

resource "aws_lambda_permission" "ingest" {
  statement_id  = "apigateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

resource "aws_lambda_permission" "api" {
  statement_id  = "apigateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}

# --- alarms and budget -----------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "dead_letters" {
  alarm_name          = "${local.name}-dead-letters"
  alarm_description   = "Events failed delivery three times and are waiting in the dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dead.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${local.name}-worker-errors"
  alarm_description   = "The worker function threw an error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.worker.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

resource "aws_budgets_budget" "monthly" {
  count        = terraform.workspace == "default" ? 1 : 0
  name         = "tailhook-monthly"
  budget_type  = "COST"
  limit_amount = "5"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}

# --- outputs ---------------------------------------------------------------

output "name" {
  value = local.name
}

output "url" {
  value = local.base_url
}

output "webhook_url" {
  value = "${local.base_url}/webhooks"
}

output "inbound_secret" {
  value     = local.inbound_secret
  sensitive = true
}
