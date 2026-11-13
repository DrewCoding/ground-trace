"""Queueing as an account: who may queue, and how many places they can hold.

The row behind a ticket is keyed by the player, so "one place per account"
is a property of the table rather than something matching has to police.
These pin down what that looks like from the API.
"""
import base64
import hashlib
import hmac
import json

from conftest import (
    call, get_session, get_ticket, heartbeat, join, leave, now, poll,
    put_session, put_ticket, queue_table,
)


def rows():
    return queue_table().scan().get("Items", [])


def decode(token):
    payload, signature = (
        base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        for part in token.split(".")
    )
    return payload, signature


# --- Who may queue ---------------------------------------------------------

def test_queue_routes_refuse_a_request_with_no_player():
    # What a route that lost its JWT authorizer would deliver. The shared key
    # is present and correct, and is not enough.
    assert call("POST", "/queue")[0] == 401
    assert call("GET", "/queue/x", params = {"ticketId": "x"})[0] == 401
    assert call("DELETE", "/queue/x", params = {"ticketId": "x"})[0] == 401
    assert rows() == []


def test_queue_routes_do_not_need_the_api_key():
    status, body = call("POST", "/queue", key = None, player = "alice")

    assert status == 200
    assert body["status"] == "waiting"


def test_a_player_cannot_poll_or_cancel_another_players_ticket():
    _, a = join("alice")

    # Mallory knows alice's ticket id, and has no ticket of their own.
    assert poll(a["ticketId"], player = "mallory")[1] == {"status": "expired"}
    assert leave(a["ticketId"], player = "mallory")[1] == {"status": "cancelled"}
    assert get_ticket(a["ticketId"])["status"] == "waiting"


# --- One place per account -------------------------------------------------

def test_joining_twice_holds_one_place(ecs):
    _, first = join("alice")
    _, second = join("alice")

    assert first["ticketId"] != second["ticketId"]
    assert len(rows()) == 1
    # One account is not two players: no server for someone queued twice.
    assert ecs.run_calls == []


def test_the_newest_client_wins():
    _, first = join("alice")
    _, second = join("alice")

    assert poll(first["ticketId"], player = "alice")[1] == {"status": "replaced"}
    assert poll(second["ticketId"], player = "alice")[1] == {"status": "waiting"}


def test_rejoining_keeps_the_place_in_line():
    put_ticket("alice", queued_ago = 120, seen_ago = 1)

    join("alice")

    assert get_ticket("alice")["queuedAt"] <= now() - 119


def test_a_replaced_client_cannot_cancel_the_new_one():
    _, first = join("alice")
    _, second = join("alice")

    assert leave(first["ticketId"], player = "alice")[1] == {"status": "replaced"}
    assert get_ticket(second["ticketId"])["status"] == "waiting"


def test_an_abandoned_ticket_is_replaced_not_resumed():
    put_ticket("alice", queued_ago = 4000, seen_ago = 4000)

    _, body = join("alice")

    assert body["status"] == "waiting"
    assert get_ticket("alice")["queuedAt"] >= now() - 1


# --- One match per account -------------------------------------------------

def matched_pair(ecs):
    _, a = join("alice")
    _, b = join("bob")
    assert len(ecs.run_calls) == 1
    return a, b, get_ticket(a["ticketId"])["sessionId"]


def test_queueing_again_while_matched_returns_the_same_server(ecs):
    a, _, session_id = matched_pair(ecs)

    _, again = join("alice")
    _, body = poll(again["ticketId"], player = "alice")

    assert len(ecs.run_calls) == 1
    assert body["status"] == "matched"
    assert body["sessionId"] == session_id
    assert poll(a["ticketId"], player = "alice")[1] == {"status": "replaced"}


def test_a_matched_ticket_cannot_be_cancelled(ecs):
    a, _, session_id = matched_pair(ecs)

    assert leave(a["ticketId"])[1] == {"status": "matched"}
    assert get_ticket(a["ticketId"])["sessionId"] == session_id


def test_a_player_the_server_reports_stays_in_the_match_past_the_grace_period():
    put_session("s1", status = "running", players = 2)
    heartbeat("s1", {"status": "running", "playerCount": 2, "players": ["alice", "bob"]})
    put_ticket("alice", status = "matched", sessionId = "s1", matchedAt = now() - 3600)

    join("alice")

    assert get_ticket("alice")["sessionId"] == "s1"


def test_a_player_the_server_no_longer_reports_can_queue_again():
    put_session("s1", status = "running", players = 1)
    heartbeat("s1", {"status": "running", "playerCount": 1, "players": ["bob"]})
    put_ticket("alice", status = "matched", sessionId = "s1", matchedAt = now() - 3600)

    _, body = join("alice")

    ticket = get_ticket("alice")
    assert body["status"] == "waiting"
    assert ticket["status"] == "waiting"
    assert "sessionId" not in ticket


def test_a_player_beaten_to_their_slot_is_not_sent_back_to_the_full_server():
    # Matched moments ago, but the server filled with two other accounts.
    put_session("s1", status = "running", players = 2)
    heartbeat("s1", {"status": "running", "playerCount": 2, "players": ["bob", "carol"]})
    put_ticket("alice", status = "matched", sessionId = "s1", matchedAt = now() - 5)

    _, body = join("alice")

    assert body["status"] == "waiting"
    assert "sessionId" not in get_ticket("alice")


def test_a_player_whose_server_is_gone_can_queue_again():
    put_ticket("alice", status = "matched", sessionId = "ended", matchedAt = now() - 30)

    join("alice")

    assert get_ticket("alice")["status"] == "waiting"


def test_a_player_whose_server_stopped_heartbeating_can_queue_again():
    put_session("s1", status = "running", players = 2, heartbeat_ago = 120)
    put_ticket("alice", status = "matched", sessionId = "s1", matchedAt = now() - 30)

    join("alice")

    assert get_ticket("alice")["status"] == "waiting"


def test_a_booting_server_holds_its_players_however_long_it_takes_to_report(ecs):
    # No heartbeat until the server is up, and a cold start can outlast the
    # staleness cutoff.
    put_session("s1", status = "provisioning", heartbeat_ago = 100, ip = None)
    put_ticket("alice", status = "matched", sessionId = "s1", matchedAt = now() - 100)

    join("alice")

    assert get_ticket("alice")["sessionId"] == "s1"


def test_a_matched_ticket_outlives_the_waiting_ttl(ecs):
    a, _, _ = matched_pair(ecs)

    assert get_ticket(a["ticketId"])["expiresAt"] >= now() + 3600

    # Polling must not pull the expiry back in.
    poll(a["ticketId"])
    assert get_ticket(a["ticketId"])["expiresAt"] >= now() + 3600


def test_a_released_ticket_goes_back_to_the_waiting_ttl(ecs):
    ecs.placement_failure = "RESOURCE:ENI"
    _, a = join("alice")
    join("bob")

    ticket = get_ticket(a["ticketId"])
    assert ticket["status"] == "waiting"
    assert ticket["expiresAt"] <= now() + 300
    assert "matchedAt" not in ticket


# --- Heartbeat player list -------------------------------------------------

def test_heartbeat_records_who_is_connected():
    put_session("s1")

    heartbeat("s1", {"status": "running", "playerCount": 2, "players": ["alice", "bob"]})

    assert get_session("s1")["players"] == ["alice", "bob"]


def test_heartbeat_drops_anything_that_is_not_a_player_id():
    put_session("s1")

    heartbeat("s1", {"status": "running", "playerCount": 1,
                     "players": ["alice", 7, None, {"x": 1}, "y" * 500]})
    assert get_session("s1")["players"] == ["alice"]

    heartbeat("s1", {"status": "running", "playerCount": 1, "players": "alice"})
    assert get_session("s1")["players"] == []


def test_a_heartbeat_without_a_player_list_still_works():
    put_session("s1")

    status, _ = heartbeat("s1", {"status": "waiting", "playerCount": 0})

    assert status == 200
    assert get_session("s1")["players"] == []


# --- Join token ------------------------------------------------------------

def test_the_join_token_names_the_player_and_the_session(ecs):
    a, _, session_id = matched_pair(ecs)

    _, body = poll(a["ticketId"])
    payload, signature = decode(body["joinToken"])
    claims = json.loads(payload)

    assert claims["sub"] == "alice"
    assert claims["sid"] == session_id
    assert now() < claims["exp"] <= now() + 60

    # Verifiable with the shared secret alone, which is all the server has.
    expected = hmac.new(b"test-join-secret", payload, hashlib.sha256).digest()
    assert hmac.compare_digest(signature, expected)


def test_each_player_gets_their_own_token(ecs):
    a, b, _ = matched_pair(ecs)

    token_a = poll(a["ticketId"])[1]["joinToken"]
    token_b = poll(b["ticketId"])[1]["joinToken"]

    assert json.loads(decode(token_a)[0])["sub"] == "alice"
    assert json.loads(decode(token_b)[0])["sub"] == "bob"
