resource "aws_apigatewayv2_api" "matchmaker" {
    name = "${var.project_name}-matchmaker"
    protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "matchmaker" {
    api_id = aws_apigatewayv2_api.matchmaker.id
    integration_type = "AWS_PROXY"
    integration_uri = aws_lambda_function.matchmaker.invoke_arn

    payload_format_version = "2.0"
}

locals {
    matchmaker_routes = [
        "POST /queue",
        "GET /queue/{ticketId}",
        "DELETE /queue/{ticketId}",
        "POST /sessions/{sessionId}/heartbeat",
        "DELETE /sessions/{sessionId}",
    ]
}

resource "aws_apigatewayv2_route" "matchmaker" {
    for_each = toset(local.matchmaker_routes)

    api_id = aws_apigatewayv2_api.matchmaker.id
    route_key = each.value
    target = "integrations/${aws_apigatewayv2_integration.matchmaker.id}"
}

resource "aws_apigatewayv2_stage" "default"{
    api_id = aws_apigatewayv2_api.matchmaker.id
    name = "$default"
    auto_deploy = true

    default_route_settings {
        throttling_rate_limit = 10
        throttling_burst_limit = 5
    }
}

resource "aws_lambda_permission" "api_gateway" {
    statement_id = "AllowAPIGatewayInvoke"
    action = "lambda:InvokeFunction"
    function_name = aws_lambda_function.function_name
    principal = "apigateway.amazonaws.com"
    source_arn = "${aws_apigatewayv2_api.matchmaker.execution_arn}/*/*"
}
