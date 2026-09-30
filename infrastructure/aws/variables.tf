variable "aws_region" {
    type = string
    default = "us-west-1"
}

variable "vpc_cidr"{
    type = string
    default = "10.0.0.0/16"
}

data "aws_availability_zones" "available" {
    state = "available"
}

variable "public_subnet_cidrs"{
    type = list(string)
    default = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs"{
    type = list(string)
    default = ["10.0.101.0/24", "10.0.102.0/24"]
}

variable "project_name" {
    type = string
    default = "ground-trace"
}

variable "github_repository" {
    type = string
    default = "DrewCoding@122519034/ground-trace@1345255887"
    description = <<-EOT
      owner/repo allowed to assume the deploy role, including GitHub's
      immutable numeric IDs. The IDs are not decoration: pinning them means a
      renamed repo - or a different repo that later takes this name - cannot
      inherit this trust. Read the exact value from the sub claim of a
      workflow's OIDC token; a plain owner/repo string will not match.
    EOT
}

variable "game_server_port"{
    type = number
    default = 7777
}

variable "game_server_cpu"{
    type = number
    default = 512
}

variable "game_server_memory"{
    type = number
    default = 1024
}

variable "game_server_image_tag"{
    type = string
    default = "latest"
}

variable "matchmaker_api_key"{
    type = string
    sensitive = true
}

variable "max_concurrent_sessions" {
    type = number
    default = 4
}
