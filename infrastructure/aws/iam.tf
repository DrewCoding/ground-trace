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
