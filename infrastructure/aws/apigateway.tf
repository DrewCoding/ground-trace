resource "aws_apigatewayv2_api" "matchmaker" {
    name = "${var.project_name}-matchmaker"
    protocol_type = "HTTP"

    # Needed for the browser dashboard. CORS is not a security control here -
    # the dashboard route is unauthenticated by design, so there is nothing an
    # origin restriction would protect. What it does do is keep browsers from
    # reaching the mutating routes cross-origin: only GET is allowed, and
    # x-api-key is not an allowed header, so a page cannot queue a player or
    # spoof a heartbeat even with a stolen key.
    # authorization must be allowed or the browser's preflight rejects the
    # dashboard's Bearer token before the request is ever sent. x-api-key is
    # deliberately absent: a page cannot reach the mutating routes
    # cross-origin even with a leaked key, since only GET is permitted.
    cors_configuration {
        allow_origins = ["*"]
        allow_methods = ["GET"]
        allow_headers = ["content-type", "authorization"]
        max_age       = 300
    }
}

resource "aws_apigatewayv2_integration" "matchmaker" {
    api_id = aws_apigatewayv2_api.matchmaker.id
    integration_type = "AWS_PROXY"
    integration_uri = aws_lambda_function.matchmaker.invoke_arn

    payload_format_version = "2.0"
}

locals {
    # GET /dashboard is declared separately - it's the only route carrying a
    # JWT authorizer, so it can't share this for_each.
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

# Gated by Cognito rather than the shared API key. The key ships inside the
# game client and is therefore recoverable; operator access shouldn't rest on
# it. API Gateway rejects an invalid token before the Lambda is ever invoked,
# so protecting the page without protecting this route would be pointless -
# the data is behind the same gate as the UI.
resource "aws_apigatewayv2_route" "dashboard" {
    api_id    = aws_apigatewayv2_api.matchmaker.id
    route_key = "GET /dashboard"
    target    = "integrations/${aws_apigatewayv2_integration.matchmaker.id}"

    authorization_type = "JWT"
    authorizer_id      = aws_apigatewayv2_authorizer.dashboard.id
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
    function_name = aws_lambda_function.matchmaker.function_name
    principal = "apigateway.amazonaws.com"
    source_arn = "${aws_apigatewayv2_api.matchmaker.execution_arn}/*/*"
}
