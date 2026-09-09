output "ecr_repository_url"{
    value = aws_ecr_repository.game_server.repository_url
}

output "sessions_table_name" {
  value = aws_dynamodb_table.sessions.name
  description = "Passed to the backend as an env var."
}