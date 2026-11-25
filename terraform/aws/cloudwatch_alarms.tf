###############################################################################
# CloudWatch alarms for the MCP server.
#
# Surfaces Lambda errors/throttles, API Gateway 5XX spikes, 4XX probing
# patterns, and Lambda duration approaching the configured timeout.
#
# If `alarm_sns_topic_arn` is set, alarms notify that topic. Otherwise the
# alarms are created with no actions — you can still see them in the
# CloudWatch console and wire notifications up later.
###############################################################################

locals {
  alarm_actions = var.alarm_sns_topic_arn != "" ? [var.alarm_sns_topic_arn] : []
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${local.lambda_name}-lambda-errors"
  alarm_description   = "Lambda function is returning errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${local.lambda_name}-lambda-throttles"
  alarm_description   = "Lambda function is being throttled (concurrency limit hit)"
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_duration_near_timeout" {
  alarm_name         = "${local.lambda_name}-lambda-duration-near-timeout"
  alarm_description  = "Lambda p95 duration is approaching the configured timeout"
  namespace          = "AWS/Lambda"
  metric_name        = "Duration"
  extended_statistic = "p95"
  period             = 300
  evaluation_periods = 2
  # 80% of the Lambda timeout. Now that the timeout is aligned just under API
  # Gateway's 29s ceiling this fires at ~22.4s -- i.e. BEFORE the gateway
  # starts returning 504s. While the timeout was 120s the threshold sat at
  # 96s, which could only trip long after users were already seeing failures.
  threshold           = local.lambda_timeout * 1000 * 0.8
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = aws_lambda_function.mcp_server.function_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

resource "aws_cloudwatch_metric_alarm" "apigw_5xx" {
  alarm_name          = "${local.lambda_name}-apigw-5xx"
  alarm_description   = "API Gateway is returning 5XX responses"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5XXError"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiName = aws_api_gateway_rest_api.mcp_api.name
    Stage   = aws_api_gateway_stage.prod.stage_name
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
}

###############################################################################
# Tool errors.
#
# A tool call that fails is still an HTTP 200 with a JSON-RPC body, so none of
# the alarms above see it. These two count log lines instead.
#
# upstream_auth: eCode360 refused the server's API key. Either the key is
# wrong or was revoked, or the publisher's gateway is refusing a good key for
# a few minutes. One occurrence is worth an email: until it clears, no tool
# can read the code. The pattern is the plugin's UPSTREAM_AUTH_LOG constant;
# tests/test_ecode_upstream_auth.py checks the two agree.
#
# tool_errors: any tool call that failed for a reason other than its
# arguments (timeouts, upstream errors, bugs). A single one is ordinary, so
# this alarms on a run of them.
###############################################################################

resource "aws_cloudwatch_log_metric_filter" "upstream_auth" {
  name           = "${local.lambda_name}-upstream-auth"
  log_group_name = aws_cloudwatch_log_group.lambda_logs.name
  pattern        = "\"upstream auth failure\""

  metric_transformation {
    name          = "${local.lambda_name}-upstream-auth"
    namespace     = "MCPServer/Tools"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "upstream_auth" {
  alarm_name          = "${local.lambda_name}-upstream-auth"
  alarm_description   = "eCode360 refused the server's API key: no tool can read the code until this clears. If it lasts, check ECODE_API_KEY / ECODE_API_SECRET."
  namespace           = "MCPServer/Tools"
  metric_name         = "${local.lambda_name}-upstream-auth"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.upstream_auth]
}

resource "aws_cloudwatch_log_metric_filter" "tool_errors" {
  name           = "${local.lambda_name}-tool-errors"
  log_group_name = aws_cloudwatch_log_group.lambda_logs.name
  pattern        = "\"Error executing tool\""

  metric_transformation {
    name          = "${local.lambda_name}-tool-errors"
    namespace     = "MCPServer/Tools"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "tool_errors" {
  alarm_name          = "${local.lambda_name}-tool-errors"
  alarm_description   = "Several tool calls failed for a reason other than their arguments (upstream errors, timeouts or a bug). See the Lambda log for 'Error executing tool'."
  namespace           = "MCPServer/Tools"
  metric_name         = "${local.lambda_name}-tool-errors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 5
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  depends_on = [aws_cloudwatch_log_metric_filter.tool_errors]
}
