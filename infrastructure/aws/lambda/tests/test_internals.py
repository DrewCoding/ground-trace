"""Races that can't be produced through the API alone."""
from mm import matching, sessions
from mm.backends import fargate

from conftest import emitted_metrics, get_session, get_ticket, join, put_session


def test_join_still_matches_when_the_index_has_not_caught_up(ecs, monkeypatch, capsys):
    _, a = join()

    # The second join's own ticket hasn't reached the GSI yet: the query sees
    # only the first player.
    first = get_ticket(a["ticketId"])
    monkeypatch.setattr(matching, "live_waiting_tickets", lambda count: [first])

    join()

    assert len(ecs.run_calls) == 1
    assert "TicketNotYetIndexed" in emitted_metrics(capsys)


def test_ip_lookup_does_not_recreate_an_ended_session(ecs):
    put_session("s1", ip = None)
    session = get_session("s1")
    sessions.end_session("s1")

    # The poll read the row just before ecs_events removed it.
    assert fargate.resolve_address(session) is None
    assert get_session("s1") is None


def test_ending_a_session_reports_the_row_exactly_once():
    put_session("s1")

    assert sessions.end_session("s1")["sessionId"] == "s1"
    assert sessions.end_session("s1") is None
