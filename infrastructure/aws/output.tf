output "ecr_repository_url"{
    value = aws_ecr_repository.game_server.repository_url
}

output "sessions_table_name" {
  value = aws_dynamodb_table.sessions.name
  description = "Passed to the backend as an env var."
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "game_server_task_definition" {
  value = aws_ecs_task_definition.game_server.family
}

output "game_server_security_group_id" {
  value = aws_security_group.game_server.id
}

output "public_subnet_ids" {
  value = module.vpc.public_subnets
}
