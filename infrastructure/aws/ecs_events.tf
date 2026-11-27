# Game server tasks stopping -> lambda/matchmaker/ecs_events.py.
#
# The only signal that a session is definitely over, whatever ended it. It
# frees the session's slot under the server cap immediately (rather than when
# the heartbeat goes stale or TTL gets to the row) and reports the task's
# billed duration.
#
# AWS service events on the default bus are free, and nothing runs between
# events - this costs nothing while nobody is playing.

resource "aws_cloudwatch_event_rule" "game_server_stopped" {
  name        = "${var.project_name}-game-server-stopped"
  description = "Game server tasks reaching STOPPED, for session cleanup and billing metrics."

  # Filtered here so the function is only invoked for events it acts on. The
  # check at the top of lambda_handler is a second line of defence.
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

# Without this, EventBridge is refused at invoke time - and nothing errors
# visibly: the function simply never runs. The rule's FailedInvocations metric
# is the only trace.
resource "aws_lambda_permission" "ecs_events" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ecs_events.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.game_server_stopped.arn
}
