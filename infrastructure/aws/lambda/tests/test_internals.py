"""Races that can't be produced through the API alone.

These call module functions directly, so unlike test_api.py they depend on
names: claims.stamp_session, claims.release_tickets, fargate.resolve_address,
and (from C7) matching.live_waiting_tickets(count).
"""
from mm import claims, matching
from mm.backends import fargate

from conftest import (
    emitted_metrics, get_session, get_ticket, join, put_session, sessions_table,
)


# --- C3: the other blind upserts --------------------------------------------
#
# A player can cancel between their ticket being claimed and being stamped or
# released; a task can stop between a poll reading its row and the IP being
# cached. An unconditional UpdateItem writes a partial row back in each case.
# One with no expiresAt is never garbage collected, and a session one counts
# against the server cap forever.

def test_stamping_a_cancelled_ticket_does_not_recreate_it():
    claims.stamp_session(["gone"], "s1")

    assert get_ticket("gone") is None


def test_releasing_a_cancelled_ticket_does_not_recreate_it():
    claims.release_tickets(["gone"])

    assert get_ticket("gone") is None


def test_ip_lookup_does_not_recreate_an_ended_session(ecs):
    put_session("s1", ip = None)
    session = get_session("s1")

    # The poll read the row just before the session ended.
    sessions_table().delete_item(Key = {"sessionId": "s1"})

    assert fargate.resolve_address(session) is None
    assert get_session("s1") is None


# --- C7: the index lags the table -------------------------------------------
#
# A GSI is updated asynchronously - AWS promises only "within a fraction of a
# second" - and join queries it milliseconds after writing the ticket. moto's
# index is never behind, so the lag is simulated by replacing the query.

def test_join_still_matches_when_the_index_has_not_caught_up(ecs, monkeypatch, capsys):
    _, a = join()

    # The second join's own ticket hasn't reached the index yet: the query
    # sees only the first player.
    first = get_ticket(a["ticketId"])
    monkeypatch.setattr(matching, "live_waiting_tickets", lambda count: [first])

    join()

    assert len(ecs.run_calls) == 1
    assert "TicketNotYetIndexed" in emitted_metrics(capsys)
