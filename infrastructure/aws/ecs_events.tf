# Game server tasks stopping -> ecs_events.py.
#
# The only signal that a session is definitely over, whatever ended it. It
# frees the session's slot under the server cap immediately (rather than when
# TTL gets around to the row) and reports the task's billed duration.
#
# AWS service events on the default bus are free, and nothing runs between
# events - this costs nothing while nobody is playing.

resource "aws_cloudwatch_event_rule" "game_server_stopped" {
  name        = "${var.project_name}-game-server-stopped"
  description = "Game server tasks reaching STOPPED, for session cleanup and billing metrics."

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.main.arn]
      lastStatus = ["STOPPED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "game_server_stopped" {
  rule = aws_cloudwatch_event_rule.game_server_stopped.name
  arn  = aws_lambda_function.ecs_events.arn
}

resource "aws_lambda_permission" "ecs_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ecs_events.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.game_server_stopped.arn
}
