resource "aws_ecs_cluster" "main" {
    name = "${var.project_name}-cluster"
}

resource "aws_cloudwatch_log_group" "game_server" {
    name = "/ecs/${var.project_name}-game-server"
    retention_in_days = 7
}

resource "aws_security_group" "game_server" {
    name = "${var.project_name}-game-server"
    vpc_id = module.vpc.vpc_id

    ingress {
        description = "Unity Transport game traffic"
        from_port = var.game_server_port
        to_port = var.game_server_port
        protocol = "udp"
        cidr_blocks = ["0.0.0.0/0"]
    }

    egress{
        from_port = 0
        to_port = 0
        protocol = "-1"
        cidr_blocks = ["0.0.0.0/0"]
    }

    tags = {
        Project = var.project_name
    }
}

resource "aws_ecs_task_definition" "game_server" {
    family = "${var.project_name}-game-server"
    requires_compatibilities = ["FARGATE"]
    network_mode = "awsvpc"
    cpu = var.game_server_cpu
    memory = var.game_server_memory

    execution_role_arn = aws_iam_role.ecs_task_execution.arn
    task_role_arn = aws_iam_role.game_server_task.arn

    container_definitions = jsonencode([
    {
        name = "game-server"
        image = "${aws_ecr_repository.game_server.repository_url}:${var.game_server_image_tag}"
        essential = true

        portMappings = [
        {
            containerPort = var.game_server_port
            hostPort = var.game_server_port
            protocol = "udp"
        }
        ]

        environment = [
            { name = "GAME_PORT", value = tostring(var.game_server_port) },
        ]

        logConfiguration = {
                logDriver = "awslogs"
                options = {
                    "awslogs-group" = aws_cloudwatch_log_group.game_server.name
                    "awslogs-region" = var.aws_region
                    "awslogs-stream-prefix" = "ecs"    
                }
        }
    }
    ])
}
