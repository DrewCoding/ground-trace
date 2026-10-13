# Entry point for ECS task state changes, delivered by EventBridge (see
# ecs_events.tf). The rule only forwards STOPPED tasks from the game server
# cluster.
#
# This is the one reliable "session over" signal. The game server never
# deregisters - an HTTP call fired while the process exits rarely completes -
# and heartbeats stopping only says a server *might* be gone. A task reaching
# STOPPED is final, whatever the cause: a normal exit, a crash, an OOM kill,
# StopTask, a Spot interruption.
import math
from datetime import datetime

from mm import metrics, sessions
from mm.config import log

# Fargate bills Linux tasks per second, rounded up, with a one-minute minimum,
# from the start of the image pull until the task stops.
MINIMUM_BILLED_SECONDS = 60


def lambda_handler(event, context):
    detail = event.get("detail") or {}
    if event.get("detail-type") != "ECS Task State Change" or detail.get("lastStatus") != "STOPPED":
        return

    session_id = launched_session_id(detail)

    if session_id:
        # Removing the row is what frees the task's slot under the server cap,
        # seconds after it stops rather than whenever TTL gets to it.
        #
        # It's also the dedupe: ECS delivers events at least once, and only
        # the first delivery gets the row back. Everything below happens once
        # per session, however many copies of the event arrive.
        if sessions.end_session(session_id) is None:
            log.info("session %s already ended; ignoring repeat event for %s",
                     session_id, detail.get("taskArn"))
            return

    stopped_at = parse_time(detail.get("stoppedAt")) or parse_time(event.get("time"))

    # Tasks started by hand (scripts/run-server.sh) have no SESSION_ID and so
    # no row to dedupe against. They're still reported - they cost money too -
    # and flagged so they can be told apart. A repeat event for one of these
    # is counted twice; that's rare, and they're rare.
    metrics.emit_metric(
        "TaskBilledSeconds", billed_seconds(detail, stopped_at), "Seconds",
        timestamp = int(stopped_at * 1000) if stopped_at else None,
        taskArn = detail.get("taskArn"),
        sessionId = session_id,
        stopCode = detail.get("stopCode"),
        stoppedReason = detail.get("stoppedReason"),
        manual = session_id is None,
    )


def launched_session_id(detail):
    """The SESSION_ID the matchmaker passed to RunTask, if there was one."""
    overrides = (detail.get("overrides") or {}).get("containerOverrides") or []
    for override in overrides:
        for env in override.get("environment") or []:
            if env.get("name") == "SESSION_ID":
                return env.get("value")
    return None


def billed_seconds(detail, stopped_at):
    started = parse_time(detail.get("pullStartedAt"))

    # No pull ever started - the task failed to place, or never got that far -
    # and billing only begins at the pull.
    if started is None or stopped_at is None:
        return 0

    return max(MINIMUM_BILLED_SECONDS, math.ceil(stopped_at - started))


def parse_time(value):
    """Epoch seconds from an ECS/EventBridge ISO timestamp, or None."""
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()
