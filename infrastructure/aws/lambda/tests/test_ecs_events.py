from datetime import datetime

import ecs_events
from conftest import (
    emitted_metrics, fill_capacity, get_session, join, poll, put_session,
)


def stopped_event(session_id = "s1", pulled = "2026-10-12T10:00:00.000Z",
                  stopped = "2026-10-12T10:05:00.500Z", stop_code = "EssentialContainerExited",
                  status = "STOPPED"):
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


def test_stopped_task_ends_its_session_and_reports_billed_time(capsys):
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(), None)

    assert get_session("s1") is None

    [metric] = emitted_metrics(capsys)["TaskBilledSeconds"]
    assert metric["TaskBilledSeconds"] == 301          # 300.5s, rounded up
    assert metric["_aws"]["Timestamp"] == epoch_ms("2026-10-12T10:05:00.500Z")
    assert metric["sessionId"] == "s1"
    assert metric["stopCode"] == "EssentialContainerExited"
    assert metric["manual"] is False


def test_repeat_deliveries_are_reported_once(capsys):
    # ECS delivers at least once; duplicates carry the same detail.version.
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(), None)
    ecs_events.lambda_handler(stopped_event(), None)

    assert len(emitted_metrics(capsys)["TaskBilledSeconds"]) == 1


def test_short_tasks_bill_the_one_minute_minimum(capsys):
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(stopped = "2026-10-12T10:00:20Z"), None)

    assert emitted_metrics(capsys)["TaskBilledSeconds"][0]["TaskBilledSeconds"] == 60


def test_task_that_never_pulled_bills_nothing(capsys):
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(pulled = None, stop_code = "TaskFailedToStart"), None)

    assert emitted_metrics(capsys)["TaskBilledSeconds"][0]["TaskBilledSeconds"] == 0


def test_missing_stopped_at_falls_back_to_the_event_time(capsys):
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(stopped = None), None)

    metric = emitted_metrics(capsys)["TaskBilledSeconds"][0]
    assert metric["TaskBilledSeconds"] == 301
    assert metric["_aws"]["Timestamp"] == epoch_ms("2026-10-12T10:05:01Z")


def test_manually_launched_tasks_are_still_reported(capsys):
    ecs_events.lambda_handler(stopped_event(session_id = None), None)

    metric = emitted_metrics(capsys)["TaskBilledSeconds"][0]
    assert metric["manual"] is True
    assert metric["sessionId"] is None


def test_other_task_states_are_ignored(capsys):
    put_session("s1")

    ecs_events.lambda_handler(stopped_event(status = "RUNNING"), None)

    assert get_session("s1") is not None
    assert emitted_metrics(capsys) == {}


def test_a_stopped_server_frees_its_slot_for_waiting_players(ecs):
    fill_capacity()
    _, a = join()
    join()
    assert ecs.run_calls == []

    ecs_events.lambda_handler(stopped_event(session_id = "full-0"), None)
    _, body = poll(a["ticketId"])

    assert len(ecs.run_calls) == 1
    assert body["status"] in ("provisioning", "matched")
