# API Gateway access logs — structured JSON to CloudWatch for per-request
# analytics (distinct users, tool usage, latency).
#
# aws_api_gateway_account is a REGION-level singleton (one CloudWatch role ARN
# per account+region). It used to be declared in every MCP stack, which made the
# stacks fight over cloudwatch_role_arn. It now lives ONLY in the mcp-stats repo
# and is pointed at a fleet-owned role, so this stack just creates the role it
# needs for its own stage and does not touch the account-level setting.

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name = "${local.lambda_name}-apigw-cloudwatch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "apigateway.amazonaws.com"
        }
      }
    ]
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# aws_api_gateway_account is intentionally NOT declared here.
#
# It is an ACCOUNT+REGION-LEVEL SINGLETON -- AWS stores exactly one CloudWatch
# role ARN for all of API Gateway in the region. It is now owned solely by the
# mcp-stats repo (terraform/aws/apigw_account.tf), which points it at the
# fleet-owned mcp-fleet-apigw-cloudwatch role.
#
# Declaring it per-MCP meant the whole fleet's API Gateway access logging hung
# off ONE MCP's IAM role, so deleting that role would have broken logging for
# every MCP -- including the log groups the mcp-stats dashboard reads.
#
# The aws_iam_role.api_gateway_cloudwatch above is no longer referenced by this
# stack. It is left in place deliberately: removing it would destroy an IAM role
# as a side effect of this refactor, and keeping it makes reverting trivial.
resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${local.lambda_name}-access"
  retention_in_days = 30

  tags = {
    Project = "mcp-server"
  }

  lifecycle {
    create_before_destroy = true
  }
}
