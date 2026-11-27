"""The defects Part C fixes, one section per step.

Every test here fails against the code Part B ended with. Each step's tests go
green when that step is done, and test_api.py must stay green throughout:
these describe what should change, test_api what must not.
"""
from conftest import (
    all_sessions, client_error, fill_capacity, get_session, get_ticket,
    heartbeat, join, now, poll, put_session, put_ticket, sessions_table,
)


# --- C1: the port came from a literal ---------------------------------------
#
# poll_ticket answered 7777 whatever the session row said. Every Fargate task
# does listen on 7777, which is why nothing noticed - but an Agones server gets
# a port from a range, and the row is the only place that knows which.

def test_poll_returns_the_sessions_own_port():
    put_session("s1", port = 7123)
    put_ticket("t", seen_ago = 0, status = "matched", sessionId = "s1")

    _, body = poll("t")

    assert body["port"] == 7123
    assert body["ip"] == "203.0.113.5"


# --- C2: heartbeats could create rows ---------------------------------------
#
# UpdateItem creates the item when the key doesn't exist. A heartbeat for a
# made-up id therefore minted a session, and every session counts toward
# MAX_CONCURRENT_SESSIONS: four requests with the recoverable API key stopped
# all matchmaking until TTL cleaned up.

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


# The same blind-upsert defect in claims.py and in the Fargate IP cache can't
# be reached through the API alone; see test_internals.py.


# --- C4: matching only ran on join ------------------------------------------
#
# Every way a match becomes possible without a new player arriving - capacity
# freeing up, a backfill slot opening, claims released after a failed launch -
# was missed, and the players already waiting sat there until someone joined.

def test_waiting_players_are_matched_once_capacity_frees(ecs):
    fill_capacity()
    _, a = join()
    join()
    assert ecs.run_calls == []

    for session in all_sessions():
        sessions_table().delete_item(Key = {"sessionId": session["sessionId"]})

    _, body = poll(a["ticketId"])

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


# --- C5: a matching failure failed the whole request ------------------------
#
# By the time matching runs, the request's own work is done: the ticket is
# written, or the status is known. A 503 from join left behind a ticket whose
# id the client never received, so nobody could poll or cancel it.

def test_a_failed_launch_does_not_fail_the_join(ecs):
    ecs.placement_failure = "RESOURCE:ENI"
    join()

    status, body = join()

    assert status == 200
    assert body["status"] == "waiting"


def test_an_aws_error_while_matching_fails_neither_join_nor_poll(ecs):
    _, a = join()
    ecs.raise_error = client_error("ThrottlingException")

    assert join()[0] == 200
    assert poll(a["ticketId"]) == (200, {"status": "waiting"})


# --- C6: liveness was measured from the wrong timestamp ---------------------
#
# Matching filtered on queuedAt, which says when a player arrived, not whether
# they're still there. It matched ghosts whose client had crashed seconds ago,
# and permanently skipped real players who had simply waited five minutes.
# lastSeenAt, refreshed by every poll, is the field that answers the question.

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


def test_a_ghost_at_the_front_of_the_queue_does_not_hide_live_players(ecs):
    # DynamoDB applies Limit to the items it reads *before* a FilterExpression
    # runs. With Limit=2 the query reads the ghost and live-1, filters out the
    # ghost, and returns one ticket - live-2 is never looked at.
    put_ticket("ghost", queued_ago = 60, seen_ago = 60)
    put_ticket("live-1", queued_ago = 30, seen_ago = 0)
    put_ticket("live-2", queued_ago = 20, seen_ago = 0)

    poll("live-1")

    assert len(ecs.run_calls) == 1
    assert get_ticket("ghost")["status"] == "waiting"
    assert get_ticket("live-1")["status"] == "matched"
    assert get_ticket("live-2")["status"] == "matched"


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
