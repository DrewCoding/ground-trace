"""Behavior that predates Phase 0 and must survive it unchanged.

These drive lambda_handler with API Gateway events rather than calling
internals, so they ran against the original single-file handler too.
"""
import boto3

from conftest import (
    PUBLIC_IP, all_sessions, call, fill_capacity, get_session, get_ticket,
    heartbeat, join, leave, poll, put_session,
)


def test_rejects_requests_without_the_api_key():
    assert call("POST", "/queue", key = None)[0] == 403
    assert call("POST", "/queue", key = "wrong")[0] == 403


def test_dashboard_is_exempt_from_the_api_key():
    # API Gateway's JWT authorizer has already vetted these requests.
    status, body = call("GET", "/dashboard", key = None)

    assert status == 200
    assert body["capacity"]["max"] == 4


def test_unknown_route_is_404():
    assert call("GET", "/nope")[0] == 404


def test_join_returns_a_waiting_ticket():
    status, body = join()

    assert status == 200
    assert body["status"] == "waiting"
    assert get_ticket(body["ticketId"])["status"] == "waiting"


def test_two_players_share_one_new_server(ecs):
    _, a = join()
    _, b = join()

    assert len(ecs.run_calls) == 1

    ticket_a, ticket_b = get_ticket(a["ticketId"]), get_ticket(b["ticketId"])
    assert ticket_a["status"] == ticket_b["status"] == "matched"
    assert ticket_a["sessionId"] == ticket_b["sessionId"]

    # The task is told which session it is, so its heartbeats land on the row.
    assert ecs.session_ids_launched() == [ticket_a["sessionId"]]
    assert get_session(ticket_a["sessionId"])["status"] == "provisioning"


def test_lone_player_does_not_get_a_server(ecs):
    _, a = join()

    assert ecs.run_calls == []
    assert poll(a["ticketId"])[1] == {"status": "waiting"}


def test_backfill_joins_a_live_server_with_room(ecs):
    put_session("live", players = 1)

    _, a = join()

    assert ecs.run_calls == []
    assert get_ticket(a["ticketId"])["sessionId"] == "live"


def test_backfill_skips_a_server_that_stopped_heartbeating(ecs):
    put_session("dead", players = 1, heartbeat_ago = 120)

    _, a = join()

    assert "sessionId" not in get_ticket(a["ticketId"])


def test_backfill_skips_a_server_still_booting(ecs):
    put_session("booting", players = 1, ip = None)

    _, a = join()

    assert "sessionId" not in get_ticket(a["ticketId"])


def test_cap_stops_provisioning(ecs):
    fill_capacity()

    _, a = join()
    _, b = join()

    assert ecs.run_calls == []
    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"


def test_poll_reports_provisioning_until_the_task_runs(ecs):
    ecs.task_status = "PROVISIONING"
    _, a = join()
    join()

    assert poll(a["ticketId"])[1] == {"status": "provisioning"}


def test_poll_resolves_and_caches_the_public_ip(ecs):
    _, a = join()
    join()

    status, body = poll(a["ticketId"])

    assert status == 200
    assert body["status"] == "matched"
    assert body["ip"] == PUBLIC_IP
    assert get_session(body["sessionId"])["publicIp"] == PUBLIC_IP


def test_placement_failure_puts_players_back_in_the_queue(ecs):
    ecs.placement_failure = "RESOURCE:ENI"

    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert all_sessions() == []


def test_poll_of_unknown_ticket_is_expired():
    assert poll("missing")[1] == {"status": "expired"}


def test_leave_removes_the_ticket():
    _, a = join()

    assert leave(a["ticketId"])[1] == {"status": "cancelled"}
    assert get_ticket(a["ticketId"]) is None

    # Polling afterwards must not write the ticket back into existence.
    assert poll(a["ticketId"])[1] == {"status": "expired"}
    assert get_ticket(a["ticketId"]) is None


def test_heartbeat_updates_a_known_session():
    put_session("s1", status = "provisioning")

    status, _ = heartbeat("s1", {"status": "running", "playerCount": 2})

    assert status == 200
    session = get_session("s1")
    assert session["status"] == "running"
    assert session["playerCount"] == 2


def test_aws_errors_surface_their_code():
    # Outside matching, an AWS refusal still reaches the caller readably.
    boto3.client("dynamodb").delete_table(TableName = "test-sessions")

    status, body = heartbeat("s1", {"status": "running", "playerCount": 1})

    assert status == 502
    assert body["code"] == "ResourceNotFoundException"
