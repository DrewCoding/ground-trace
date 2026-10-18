"""The Agones backend, driven through the API like the Fargate tests.

The allocator is faked with the response shapes a real Agones 1.61 allocator
returned when probed (docs/phase-2.md).
"""
import io
import json
import urllib.error

import pytest

from mm.backends import agones as agones_backend
from mm.responses import MatchmakerError

from conftest import (
    ALLOCATED_IP, all_sessions, emitted_metrics, fill_capacity, get_session,
    get_ticket, heartbeat, join, poll, put_session,
)


def test_matched_players_get_an_address_without_waiting(agones, ecs):
    _, a = join()
    join()

    # The allocation already carried the address, so the very next poll is
    # "matched" - the provisioning wait is what this backend removes.
    status, body = poll(a["ticketId"])

    assert status == 200
    assert body == {"status": "matched", "sessionId": "ground-trace-abc12-gs1",
                    "ip": ALLOCATED_IP, "port": 7001}
    assert ecs.run_calls == []


def test_allocation_asks_the_fleet_for_a_server(agones):
    join()
    join()

    assert agones.requests == [{
        "namespace": "default",
        "gameServerSelectors": [{"matchLabels": {"agones.dev/fleet": "ground-trace"}}],
    }]


def test_session_row_is_tagged_and_not_joinable(agones):
    _, a = join()
    join()

    session = get_session(get_ticket(a["ticketId"])["sessionId"])

    assert session["orchestrator"] == "agones"
    assert session["status"] == "allocated"
    assert session["publicIp"] == ALLOCATED_IP


def test_empty_warm_pool_leaves_players_waiting(agones, capsys):
    agones.status = 429
    _, a = join()
    _, b = join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"
    assert all_sessions() == []

    metrics = emitted_metrics(capsys)
    assert "WarmPoolExhausted" in metrics
    # An empty pool is expected, not a fault.
    assert "ProvisionFailures" not in metrics
    assert "MatchingErrors" not in metrics
    # Released claims aren't matches.
    assert "TimeToMatchSeconds" not in metrics


def test_players_are_matched_once_a_server_is_ready(agones):
    agones.status = 429
    _, a = join()
    join()

    agones.status = 200
    _, body = poll(a["ticketId"])

    assert body["status"] == "matched"


def test_contention_is_retried_like_an_empty_pool(agones, capsys):
    agones.status = 409
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    metrics = emitted_metrics(capsys)
    assert "AllocationContention" in metrics
    assert "ProvisionFailures" not in metrics


def test_allocator_errors_release_players_and_are_counted(agones, capsys):
    agones.status = 500
    _, a = join()
    status, b = join()

    # The join itself still succeeds - matching retries on the next poll.
    assert status == 200
    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"
    assert "ProvisionFailures" in emitted_metrics(capsys)


def test_unreachable_allocator_releases_players(agones, capsys):
    agones.raise_error = MatchmakerError("allocator unreachable: connection refused")
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert "MatchingErrors" in emitted_metrics(capsys)


def test_fargate_rows_do_not_cap_agones(agones):
    # The Fleet's size bounds this path, not the session table.
    fill_capacity()
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "matched"


def test_leftover_agones_rows_do_not_cap_fargate(ecs):
    # Nothing deletes Agones rows when their server ends; after switching
    # back to fargate they must not stop matchmaking until TTL gets to them.
    for i in range(4):
        put_session(f"gs-{i}", status = "allocated", orchestrator = "agones")

    join()
    join()

    assert len(ecs.run_calls) == 1


def test_lone_player_is_not_backfilled_before_the_server_reports(agones):
    join()
    join()
    _, c = join()

    # Until the server's first heartbeat, its playerCount is the matchmaker's
    # guess. Offering it would send a third player into what may be a full 1v1.
    _, body = poll(c["ticketId"])

    assert body == {"status": "waiting"}
    assert len(agones.requests) == 1


def test_lone_player_is_backfilled_once_the_server_reports_a_free_slot(agones):
    _, a = join()
    join()
    session_id = get_ticket(a["ticketId"])["sessionId"]

    # One player left; the server's heartbeat, sent under its GameServer
    # name, says so.
    status, _ = heartbeat(session_id, {"status": "waiting", "playerCount": 1})
    assert status == 200

    _, c = join()
    _, body = poll(c["ticketId"])

    # Through the same find_joinable_session path Fargate uses - no second
    # allocation.
    assert body["status"] == "matched"
    assert body["sessionId"] == session_id
    assert len(agones.requests) == 1


# --- AllocatorClient: the real HTTP handling -------------------------------

def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://allocator.test/gameserverallocation", code, "error", {}, io.BytesIO(body))


@pytest.fixture
def client(monkeypatch):
    real = agones_backend.AllocatorClient()
    monkeypatch.setattr(real, "_tls", lambda: None)
    return real


def test_client_returns_error_statuses_instead_of_raising(client, monkeypatch):
    body = json.dumps({"code": 8, "message": "there is no available GameServer to allocate"}).encode()

    def urlopen(req, context, timeout):
        raise _http_error(429, body)

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    assert client.allocate({}) == (429, {"code": 8, "message": "there is no available GameServer to allocate"})


def test_client_tolerates_a_non_json_error_body(client, monkeypatch):
    def urlopen(req, context, timeout):
        raise _http_error(502, b"<html>bad gateway</html>")

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    assert client.allocate({}) == (502, {"message": "<html>bad gateway</html>"})


def test_client_reports_why_the_allocator_was_unreachable(client, monkeypatch):
    def urlopen(req, context, timeout):
        raise urllib.error.URLError("tlsv13 alert certificate required")

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    with pytest.raises(MatchmakerError, match = "certificate required"):
        client.allocate({})


def test_missing_certificate_is_not_reported_as_unreachable():
    # The real _tls(), pointed at conftest's nonexistent paths.
    with pytest.raises(FileNotFoundError):
        agones_backend.AllocatorClient().allocate({})
