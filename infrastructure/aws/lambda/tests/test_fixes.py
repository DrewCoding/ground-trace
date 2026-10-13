"""Regression tests for the defects Phase 0 fixed.

Each of these failed against the original handler. See docs/phase-0.md for
the analysis behind them.
"""
from conftest import (
    all_sessions, client_error, fill_capacity, get_session, get_ticket,
    heartbeat, join, now, poll, put_session, put_ticket, sessions_table,
)


# --- Matching only ran on join --------------------------------------------

def test_waiting_players_are_matched_once_capacity_frees(ecs):
    fill_capacity()
    _, a = join()
    join()
    assert ecs.run_calls == []

    for session in all_sessions():
        sessions_table().delete_item(Key = {"sessionId": session["sessionId"]})

    status, body = poll(a["ticketId"])

    assert len(ecs.run_calls) == 1
    assert body["status"] in ("provisioning", "matched")


def test_lone_player_is_backfilled_when_a_slot_opens_later(ecs):
    _, a = join()
    put_session("live", players = 1)

    _, body = poll(a["ticketId"])

    assert ecs.run_calls == []
    assert body == {"status": "matched", "sessionId": "live",
                    "ip": "203.0.113.5", "port": 7777}


def test_players_released_after_a_failed_launch_are_retried(ecs):
    ecs.placement_failure = "RESOURCE:ENI"
    _, a = join()
    join()
    assert get_ticket(a["ticketId"])["status"] == "waiting"

    ecs.placement_failure = None
    poll(a["ticketId"])

    assert get_ticket(a["ticketId"])["status"] == "matched"
    assert len(all_sessions()) == 1


def test_a_failed_launch_does_not_fail_the_join(ecs):
    ecs.placement_failure = "RESOURCE:ENI"
    join()

    status, body = join()

    # The ticket was recorded; matching just didn't succeed yet. A 503 here
    # used to strand a ticket the client never learned the id of.
    assert status == 200
    assert body["status"] == "waiting"


def test_an_aws_error_while_matching_fails_neither_join_nor_poll(ecs):
    _, a = join()
    ecs.raise_error = client_error("ThrottlingException")

    assert join()[0] == 200
    assert poll(a["ticketId"]) == (200, {"status": "waiting"})


# --- Heartbeats could create rows ----------------------------------------

def test_heartbeat_for_an_unknown_session_is_rejected():
    status, _ = heartbeat("made-up", {"status": "waiting", "playerCount": 0})

    assert status == 404
    assert get_session("made-up") is None


def test_fake_heartbeats_cannot_exhaust_capacity(ecs):
    for i in range(6):
        heartbeat(f"fake-{i}", {"status": "running", "playerCount": 2})

    join()
    join()

    assert len(ecs.run_calls) == 1


# --- Ticket liveness -------------------------------------------------------

def test_a_player_who_stopped_polling_is_not_matched(ecs):
    # Queued recently, but the client crashed and stopped polling 30s ago.
    put_ticket("ghost", queued_ago = 30, seen_ago = 30)

    _, b = join()

    assert ecs.run_calls == []
    assert get_ticket("ghost")["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"


def test_a_patient_player_stays_matchable(ecs):
    # Waiting longer than the ticket TTL, but still polling.
    put_ticket("patient", queued_ago = 400, seen_ago = 1)

    join()

    assert len(ecs.run_calls) == 1
    assert get_ticket("patient")["status"] == "matched"


def test_polling_keeps_a_ticket_alive():
    put_ticket("t", queued_ago = 200, seen_ago = 8)

    poll("t")

    ticket = get_ticket("t")
    assert ticket["lastSeenAt"] >= now() - 1
    assert ticket["expiresAt"] >= now() + 299


def test_a_ticket_nobody_has_polled_past_its_ttl_is_expired():
    # TTL deletion is best-effort, so the row can still be there.
    put_ticket("abandoned", queued_ago = 400, seen_ago = 400)

    assert poll("abandoned")[1] == {"status": "expired"}


# --- Port came from a literal ----------------------------------------------

def test_poll_returns_the_sessions_own_port():
    put_session("s1", port = 7123)
    put_ticket("t", seen_ago = 0, status = "matched", sessionId = "s1")

    _, body = poll("t")

    assert body["port"] == 7123
    assert body["ip"] == "203.0.113.5"
