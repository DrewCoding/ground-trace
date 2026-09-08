resource "aws_ecr_repository" "game_server"{
    name = "${var.project_name}-game-server"
    image_tag_mutability = "MUTABLE"

    image_scanning_configuration{
        scan_on_push = true
    }
}

resource "aws_ecr_lifecycle_policy" "game_server"{
    repository = aws_ecr_repository.game_server.name

    policy = jsonencode({
        rulePriority = 1
        description  = "Keep only the 5 most recent images"
        selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
        }
        action = {
            type = "expire"
        }
    })
}
