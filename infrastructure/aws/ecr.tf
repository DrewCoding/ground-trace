resource "aws_ecr_repository" "game_server"{
    name = "${var.project_name}-game-server"
    image_tag_mutability = "MUTABLE"

    image_scanning_configuration{
        scan_on_push = true
    }
}
