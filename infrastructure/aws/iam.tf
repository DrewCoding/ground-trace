data "aws_iam_policy_document" "ecs_tasks_assume"{
    statement{
        effect = "Allow"
        actions = ["sts:AssumeRole"]

        principals{
            type = "Service"
            identifiers = ["ecs-tasks.amazonaws.com"]
        }
    }
}

resource "aws_iam_role" "ecs_task_execution" {
    name = "${var.project_name}-ecs-task-execution"
    assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_task_execution" {
    role = aws_iam_role.ecs_task_execution.name
    policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "game_server_task" {
    name = "${var.project_name}-game-server-task"
    assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "lambda_assume" {
    statement {
        effect  = "Allow"
        actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "matchmaker" {
    name = "${var.project_name}-matchmaker"
    assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "matchmaker_logs" {
    role = aws_iam_role.matchmaker.name
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "matchmaker" {

    statement {
        sid    = "MatchmakerTables"
        effect = "Allow"
        actions = [
            "dynamodb:Scan",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        ]
    resources = [
            aws_dynamodb_table.sessions.arn,
            "${aws_dynamodb_table.sessions.arn}/index/*",
            aws_dynamodb_table.queue.arn,
            "${aws_dynamodb_table.queue.arn}/index/*",
        ]
    }

    statement {
        sid = "LaunchGameServers"
        effect = "Allow"
        actions = [
            "ecs:RunTask",
            "ecs:DescribeTasks",
            "ecs:StopTask",
        ]
        resources = ["*"]
    }

    statement {
        sid = "PassTaskRoles"
        effect = "Allow"
        actions = ["iam:PassRole"]
        resources = [
            aws_iam_role.ecs_task_execution.arn,
            aws_iam_role.game_server_task.arn,
        ]
  }

    statement {
        sid = "ResolveTaskPublicIp"
        effect = "Allow"
        actions = ["ec2:DescribeNetworkInterfaces"]
        resources = ["*"]
    }
}

resource "aws_iam_role_policy" "matchmaker" {
    name = "${var.project_name}-matchmaker"
    role = aws_iam_role.matchmaker.id
    policy = data.aws_iam_policy_document.matchmaker.json
}

# The ECS task-state listener. Separate from the matchmaker role so a function
# that only ever deletes session rows can't launch tasks or read the queue.
resource "aws_iam_role" "ecs_events" {
    name = "${var.project_name}-ecs-events"
    assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "ecs_events_logs" {
    role = aws_iam_role.ecs_events.name
    policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "ecs_events" {
    statement {
        sid = "EndSessions"
        effect = "Allow"
        actions = ["dynamodb:DeleteItem"]
        resources = [aws_dynamodb_table.sessions.arn]
    }
}

resource "aws_iam_role_policy" "ecs_events" {
    name = "${var.project_name}-ecs-events"
    role = aws_iam_role.ecs_events.id
    policy = data.aws_iam_policy_document.ecs_events.json
}
