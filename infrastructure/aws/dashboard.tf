# Operational dashboard. The custom metrics come from the Lambda via
# Embedded Metric Format - see emit_metric() in handler.py.
#
# Grid is 24 columns wide; x/y/width/height position each widget.

locals {
  matchmaker_namespace = "GroundTrace/Matchmaker"
}

resource "aws_cloudwatch_dashboard" "matchmaker" {
  dashboard_name = "${var.project_name}-matchmaker"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Match funnel"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          stat   = "Sum"
          metrics = [
            [local.matchmaker_namespace, "TicketsCreated"],
            [".", "MatchesFormed"],
            [".", "ServersProvisioned"],
            [".", "SessionsBackfilled"],
          ]
        }
      },

      {
        # ServersProvisioned vs SessionsBackfilled is the ratio that says how
        # much the backfill path is actually saving in cold starts.
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Provision vs backfill (cumulative)"
          view   = "singleValue"
          region = var.aws_region
          period = 86400
          stat   = "Sum"
          metrics = [
            [local.matchmaker_namespace, "ServersProvisioned"],
            [".", "SessionsBackfilled"],
            [".", "MatchesFormed"],
          ]
        }
      },

      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Cold start: RunTask to reachable address"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          metrics = [
            [local.matchmaker_namespace, "ProvisionDurationSeconds", { stat = "Average", label = "avg" }],
            ["...", { stat = "Maximum", label = "max" }],
            ["...", { stat = "p90", label = "p90" }],
          ]
        }
      },

      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Time to match (player-perceived wait)"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          metrics = [
            [local.matchmaker_namespace, "TimeToMatchSeconds", { stat = "Average", label = "avg" }],
            ["...", { stat = "Maximum", label = "max" }],
          ]
        }
      },

      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "Running game servers"
          view   = "timeSeries"
          region = var.aws_region
          period = 60
          stat   = "Average"
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ClusterName", aws_ecs_cluster.main.name],
          ]
        }
      },

      {
        # CapacityCapHit means demand exceeded a deliberate cost ceiling -
        # that's different from something being broken, and worth separating.
        type   = "metric"
        x      = 8
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "Failure and contention signals"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          stat   = "Sum"
          metrics = [
            [local.matchmaker_namespace, "ProvisionFailures"],
            [".", "CapacityCapHit"],
            [".", "ClaimContention"],
          ]
        }
      },

      {
        type   = "metric"
        x      = 16
        y      = 12
        width  = 8
        height = 6
        properties = {
          title  = "Lambda health"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.matchmaker.function_name, { stat = "Sum" }],
            [".", "Errors", ".", ".", { stat = "Sum" }],
            [".", "Throttles", ".", ".", { stat = "Sum" }],
            [".", "Duration", ".", ".", { stat = "Average", yAxis = "right" }],
          ]
        }
      },

      {
        # HTTP APIs publish lowercase 4xx/5xx. REST APIs use 4XXError/5XXError -
        # using the wrong pair silently graphs nothing.
        type   = "metric"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiId", aws_apigatewayv2_api.matchmaker.id, { stat = "Sum" }],
            [".", "4xx", ".", ".", { stat = "Sum" }],
            [".", "5xx", ".", ".", { stat = "Sum" }],
            [".", "IntegrationLatency", ".", ".", { stat = "Average", yAxis = "right" }],
          ]
        }
      },

      {
        type   = "metric"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "DynamoDB consumed capacity"
          view   = "timeSeries"
          region = var.aws_region
          period = 300
          stat   = "Sum"
          metrics = [
            ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", aws_dynamodb_table.queue.name],
            [".", "ConsumedWriteCapacityUnits", ".", "."],
            [".", "ConsumedReadCapacityUnits", ".", aws_dynamodb_table.sessions.name],
            [".", "ConsumedWriteCapacityUnits", ".", "."],
          ]
        }
      },
    ]
  })
}

# --- Alarms ---
#
# No SNS actions wired up yet, so these are visible state rather than
# notifications. Adding an SNS topic with an email subscription is the
# obvious next step.

resource "aws_cloudwatch_metric_alarm" "provision_failures" {
  alarm_name          = "${var.project_name}-provision-failures"
  alarm_description   = "RunTask could not place a game server."
  namespace           = local.matchmaker_namespace
  metric_name         = "ProvisionFailures"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.project_name}-matchmaker-errors"
  alarm_description   = "Matchmaker Lambda threw an unhandled exception."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.matchmaker.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.project_name}-api-5xx"
  alarm_description   = "Matchmaker API returning server errors."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.matchmaker.id }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
}

output "dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards/dashboard/${aws_cloudwatch_dashboard.matchmaker.dashboard_name}"
}
