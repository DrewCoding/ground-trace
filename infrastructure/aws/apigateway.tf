resource "aws_apigatewayv2_api" "matchmaker" {
    name = "${var.project_name}-matchmaker"
    protocol_type = "HTTP"

    # Needed for the browser dashboard, which is the only thing that calls
    # this API from a web page. authorization must be allowed or the
    # browser's preflight rejects the dashboard's Bearer token before the
    # request is ever sent.
    #
    # Only GET is allowed, which keeps a page from reaching the mutating
    # routes cross-origin: it cannot queue a player with a token it got hold
    # of, and x-api-key is not an allowed header, so it cannot spoof a
    # heartbeat with a leaked key either.
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
    # Grouped by who calls them, because that decides how each is
    # authenticated - and handler.py makes the same split by path, so a route
    # moved between these lists has to move there too.

    # Players. Carry the player pool's JWT authorizer (cognito_players.tf).
    queue_routes = [
        "POST /queue",
        "GET /queue/{ticketId}",
        "DELETE /queue/{ticketId}",
    ]

    # Game servers. No authorizer: the handler checks the shared x-api-key,
    # which only the servers hold now that the client signs in instead.
    session_routes = [
        "POST /sessions/{sessionId}/heartbeat",
        "DELETE /sessions/{sessionId}",
    ]
}

# An invalid or missing token is refused here, before the Lambda is invoked,
# so an anonymous caller can't even make the matchmaker run.
resource "aws_apigatewayv2_route" "queue" {
    for_each = toset(local.queue_routes)

    api_id = aws_apigatewayv2_api.matchmaker.id
    route_key = each.value
    target = "integrations/${aws_apigatewayv2_integration.matchmaker.id}"

    authorization_type = "JWT"
    authorizer_id = aws_apigatewayv2_authorizer.players.id
}

resource "aws_apigatewayv2_route" "sessions" {
    for_each = toset(local.session_routes)

    api_id = aws_apigatewayv2_api.matchmaker.id
    route_key = each.value
    target = "integrations/${aws_apigatewayv2_integration.matchmaker.id}"
}

# The routes above used to be one resource. Without these, Terraform would
# destroy and recreate all five instead of updating them in place, and every
# request in between would 404.
moved {
    from = aws_apigatewayv2_route.matchmaker["POST /queue"]
    to = aws_apigatewayv2_route.queue["POST /queue"]
}

moved {
    from = aws_apigatewayv2_route.matchmaker["GET /queue/{ticketId}"]
    to = aws_apigatewayv2_route.queue["GET /queue/{ticketId}"]
}

moved {
    from = aws_apigatewayv2_route.matchmaker["DELETE /queue/{ticketId}"]
    to = aws_apigatewayv2_route.queue["DELETE /queue/{ticketId}"]
}

moved {
    from = aws_apigatewayv2_route.matchmaker["POST /sessions/{sessionId}/heartbeat"]
    to = aws_apigatewayv2_route.sessions["POST /sessions/{sessionId}/heartbeat"]
}

moved {
    from = aws_apigatewayv2_route.matchmaker["DELETE /sessions/{sessionId}"]
    to = aws_apigatewayv2_route.sessions["DELETE /sessions/{sessionId}"]
}

# Gated by the operator pool (cognito.tf), not the player pool and not the
# shared API key: being able to play, or being a game server, is no reason to
# see the fleet. API Gateway rejects an invalid token before the Lambda is ever invoked,
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
