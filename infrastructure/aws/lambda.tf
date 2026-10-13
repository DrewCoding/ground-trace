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
        variables = {
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
