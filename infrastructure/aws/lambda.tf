# One zip, two functions: the API (handler.py) and the ECS task-state
# listener (ecs_events.py) share the mm package, so they deploy together.
data "archive_file" "matchmaker" {
    type = "zip"
    source_dir = "${path.module}/lambda/matchmaker"
    output_path = "${path.module}/build/matchmaker.zip"

    # Bytecode from a local run would otherwise ship, and change the hash -
    # and so redeploy both functions - on every machine that ran the tests.
    excludes = ["**/__pycache__/**", "**/*.pyc"]
}

# The allocator's CA, client cert and key, as one SecureString. Deliberately
# not a Terraform resource: Terraform would store the value - a private key -
# in state, so it's uploaded out of band by scripts/put-allocator-tls.sh, the
# same way the cluster's Secrets are. Parameter Store rather than Secrets
# Manager because its standard tier is free; a secret costs $0.40 a month
# whether or not a cluster exists.
locals {
    agones_tls_parameter = "/${var.project_name}/agones/allocator-tls"
}

resource "aws_cloudwatch_log_group" "matchmaker" {
    name = "/aws/lambda/${var.project_name}-matchmaker"
    retention_in_days = 7
}

resource "aws_lambda_function" "matchmaker" {
    function_name = "${var.project_name}-matchmaker"
    role = aws_iam_role.matchmaker.arn
    handler = "handler.lambda_handler"
    runtime = "python3.13"

    filename = data.archive_file.matchmaker.output_path

    source_code_hash = data.archive_file.matchmaker.output_base64sha256

    timeout = 30
    memory_size = 256

    environment {
        # The AGONES_* settings only when they're used. mm.backends.agones
        # reads them at import, and only an agones deploy imports it.
        variables = merge({
          SESSIONS_TABLE = aws_dynamodb_table.sessions.name
          QUEUE_TABLE = aws_dynamodb_table.queue.name
          ECS_CLUSTER = aws_ecs_cluster.main.name
          TASK_DEFINITION = aws_ecs_task_definition.game_server.family
          SUBNET_IDS = join(",", module.vpc.public_subnets)
          SECURITY_GROUP_ID = aws_security_group.game_server.id
          CONTAINER_NAME = "game-server"
          GAME_PORT = tostring(var.game_server_port)
          API_KEY = var.matchmaker_api_key
          MAX_CONCURRENT_SESSIONS = tostring(var.max_concurrent_sessions)
          ORCHESTRATOR = var.orchestrator
        }, var.orchestrator == "agones" ? {
          AGONES_ALLOCATOR_URL = var.agones_allocator_url
          AGONES_TLS_PARAMETER = local.agones_tls_parameter
        } : {})
    }

    lifecycle {
        # Caught at plan time rather than as a KeyError on every cold start.
        precondition {
            condition = var.orchestrator != "agones" || startswith(var.agones_allocator_url, "https://")
            error_message = "orchestrator = agones needs agones_allocator_url (https://...) from the EKS bootstrap."
        }
    }
}

resource "aws_cloudwatch_log_group" "ecs_events" {
    name = "/aws/lambda/${var.project_name}-ecs-events"
    retention_in_days = 7
}

resource "aws_lambda_function" "ecs_events" {
    function_name = "${var.project_name}-ecs-events"
    role = aws_iam_role.ecs_events.arn
    handler = "ecs_events.lambda_handler"
    runtime = "python3.13"

    filename = data.archive_file.matchmaker.output_path

    source_code_hash = data.archive_file.matchmaker.output_base64sha256

    timeout = 10
    memory_size = 128

    # Only what mm.config requires at import. Deliberately no API key and no
    # launch settings: this function never serves requests or starts tasks.
    environment {
        variables = {
          SESSIONS_TABLE = aws_dynamodb_table.sessions.name
          QUEUE_TABLE = aws_dynamodb_table.queue.name
        }
    }
}
