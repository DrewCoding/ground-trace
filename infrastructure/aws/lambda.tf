data "archive_file" "matchmaker" {
    type = "zip"
    source_dir = "${path.module}/lambda/matchmaker"
    output_path = "${path.module}/build/matchmaker.zip"
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
        }
    }
}


