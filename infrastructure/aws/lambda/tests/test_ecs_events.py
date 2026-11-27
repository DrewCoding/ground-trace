"""The ECS task-state listener: ecs_events.lambda_handler, fed EventBridge events.

ecs_events is imported inside each test rather than at the top, so that while
it doesn't exist yet each test fails on its own instead of the whole run
stopping at collection.
"""
import os
import subprocess
import sys
from datetime import datetime

from mm import sessions

from conftest import (
    MATCHMAKER_DIR, emitted_metrics, fill_capacity, get_session, join, poll, put_session,
)



def handle(event):
    import ecs_events
    return ecs_events.lambda_handler(event, None)


def stopped_event(session_id = "s1", pulled = "2026-10-12T10:00:00.000Z",
                  stopped = "2026-10-12T10:05:00.500Z", stop_code = "EssentialContainerExited",
                  status = "STOPPED"):
    """The shape EventBridge delivers for an ECS Task State Change."""
    environment = [{"name": "SESSION_ID", "value": session_id}] if session_id else []
    detail = {
        "taskArn": "arn:aws:ecs:us-west-1:123456789012:task/test-cluster/abc",
        "lastStatus": status,
        "stopCode": stop_code,
        "stoppedReason": "Essential container in task exited",
        "overrides": {"containerOverrides": [{"name": "game-server", "environment": environment}]},
        "version": 7,
    }
    if pulled:
        detail["pullStartedAt"] = pulled
    if stopped:
        detail["stoppedAt"] = stopped
    return {
        "detail-type": "ECS Task State Change",
        "source": "aws.ecs",
        "time": "2026-10-12T10:05:01Z",
        "detail": detail,
    }


def epoch_ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


# --- D1: ending sessions ----------------------------------------------------

def test_ending_a_session_returns_the_row_exactly_once():
    # The dedupe point: only one caller can get the row back.
    put_session("s1")

    assert sessions.end_session("s1")["sessionId"] == "s1"
    assert sessions.end_session("s1") is None


def test_stopped_task_ends_its_session():
    put_session("s1")

    handle(stopped_event())

    assert get_session("s1") is None


def test_other_task_states_are_ignored(capsys):
    put_session("s1")

    handle(stopped_event(status = "RUNNING"))

    assert get_session("s1") is not None
    assert emitted_metrics(capsys) == {}


def test_a_stopped_server_frees_its_slot_for_waiting_players(ecs):
    fill_capacity()
    _, a = join()
    join()
    assert ecs.run_calls == []

    handle(stopped_event(session_id = "full-0"))
    _, body = poll(a["ticketId"])

    assert len(ecs.run_calls) == 1
    assert body["status"] in ("provisioning", "matched")


# --- D2: billed time --------------------------------------------------------
#
# Fargate bills Linux tasks per second, rounded up, with a one-minute minimum,
# from the start of the image pull until the task stops.

def test_stopped_task_reports_its_billed_time(capsys):
    put_session("s1")

    handle(stopped_event())

    [metric] = emitted_metrics(capsys)["TaskBilledSeconds"]
    assert metric["TaskBilledSeconds"] == 301          # 300.5s, rounded up
    assert metric["sessionId"] == "s1"
    assert metric["stopCode"] == "EssentialContainerExited"
    assert metric["manual"] is False


def test_billed_time_is_dated_when_the_task_stopped(capsys):
    # EMF's Timestamp places the data point. Left as "now", a delayed event
    # would land in the wrong period on the dashboard.
    put_session("s1")

    handle(stopped_event())

    [metric] = emitted_metrics(capsys)["TaskBilledSeconds"]
    assert metric["_aws"]["Timestamp"] == epoch_ms("2026-10-12T10:05:00.500Z")


def test_repeat_deliveries_are_reported_once(capsys):
    # EventBridge delivers at least once.
    put_session("s1")

    handle(stopped_event())
    handle(stopped_event())

    assert len(emitted_metrics(capsys)["TaskBilledSeconds"]) == 1


def test_short_tasks_bill_the_one_minute_minimum(capsys):
    put_session("s1")

    handle(stopped_event(stopped = "2026-10-12T10:00:20Z"))

    assert emitted_metrics(capsys)["TaskBilledSeconds"][0]["TaskBilledSeconds"] == 60


def test_task_that_never_pulled_bills_nothing(capsys):
    put_session("s1")

    handle(stopped_event(pulled = None, stop_code = "TaskFailedToStart"))

    assert emitted_metrics(capsys)["TaskBilledSeconds"][0]["TaskBilledSeconds"] == 0


def test_missing_stopped_at_falls_back_to_the_event_time(capsys):
    put_session("s1")

    handle(stopped_event(stopped = None))

    metric = emitted_metrics(capsys)["TaskBilledSeconds"][0]
    assert metric["TaskBilledSeconds"] == 301
    assert metric["_aws"]["Timestamp"] == epoch_ms("2026-10-12T10:05:01Z")


def test_manually_launched_tasks_are_still_reported(capsys):
    # scripts/run-server.sh starts tasks with no SESSION_ID, so no row.
    handle(stopped_event(session_id = None))

    metric = emitted_metrics(capsys)["TaskBilledSeconds"][0]
    assert metric["manual"] is True
    assert metric["sessionId"] is None


# --- D3: deployable with its own, smaller environment -----------------------
#
# conftest sets every variable the API function needs, so the tests above
# can't notice if ecs_events depends on one it won't have. This imports it in
# a fresh interpreter with only what its Terraform will set. -B keeps that
# interpreter from writing bytecode into the folder that gets zipped.

def test_ecs_events_imports_with_only_the_table_settings():
    env = {
        "PATH": os.environ.get("PATH", ""),
        # What the Lambda runtime always provides. The credentials are never
        # used - nothing calls AWS at import - but without them boto3 goes
        # looking through the credential chain for some.
        "AWS_DEFAULT_REGION": "us-west-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_CONFIG_FILE": os.devnull,
        "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
        # What ecs_events.tf sets. Nothing else.
        "SESSIONS_TABLE": "test-sessions",
        "QUEUE_TABLE": "test-queue",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", "import ecs_events"],
        cwd = MATCHMAKER_DIR, env = env, capture_output = True, text = True,
    )

    assert result.returncode == 0, result.stderr
