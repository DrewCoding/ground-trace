"""Turning waiting tickets into matches.

try_form_match evaluates the queue as it stands and does whatever is possible
right now. It is level-triggered: callers run it on every join and every
waiting poll, not just when a player arrives. Run only on join, it missed
every way a match becomes possible without one - capacity freeing up, a
backfill slot opening, claims released after a lost race or a failed launch -
and the players already waiting sat there until someone new showed up.

Running it more often is safe: claims are conditional writes, so concurrent
invocations can't both take a ticket, and a losing one just leaves things
for the next poll.
"""
import time

from boto3.dynamodb.conditions import Attr, Key

from mm import backends, claims, config, ddb, metrics, sessions


def try_form_match(new_ticket = None):
    """Match whatever the queue allows. True if any ticket was claimed.

    new_ticket is a ticket the caller has just written. Its index entry is
    updated asynchronously - AWS only says "within a fraction of a second" -
    and the query runs milliseconds after the write, so it can be missing.
    The caller knows it exists, so it's included either way.
    """
    waiting = live_waiting_tickets(config.PLAYERS_PER_MATCH)

    if (new_ticket and len(waiting) < config.PLAYERS_PER_MATCH
            and all(t["ticketId"] != new_ticket["ticketId"] for t in waiting)):
        waiting.append(new_ticket)
        # Measures how often the index lag actually bites.
        metrics.emit_metric("TicketNotYetIndexed", 1)

    if not waiting:
        return False

    # Backfill before provisioning. A server that's already up with a free
    # slot needs only enough players to fill it - not a whole match - so a
    # lone queued player can join a 1v1 whose opponent left, instead of
    # waiting for a partner and paying for a second container.
    session = sessions.find_joinable_session()
    if session:
        free_slots = int(session.get("maxPlayers", config.PLAYERS_PER_MATCH)) - int(session.get("playerCount", 0))
        claimed = assign_tickets(waiting[:free_slots], session["sessionId"])

        # The metric that measures what backfill is actually worth: every one
        # of these is a container that didn't have to be cold-started.
        if claimed:
            metrics.emit_metric("SessionsBackfilled", 1,
                                sessionId = session["sessionId"], players = len(claimed))
        return bool(claimed)

    # Nothing to join, so a new server has to be worth it: only provision
    # once there are enough players for a full match.
    if len(waiting) < config.PLAYERS_PER_MATCH:
        return False

    if not backends.active.has_capacity():
        # Leave everyone queued rather than provisioning past the cap. Their
        # next poll tries again, so they're matched once a server stops.
        metrics.emit_metric("CapacityCapHit", 1)
        return False

    claimed = []
    for ticket in waiting[:config.PLAYERS_PER_MATCH]:
        if claims.claim_ticket(ticket["ticketId"]):
            claimed.append(ticket)
        else:
            claims.release_tickets([t["ticketId"] for t in claimed])
            metrics.emit_metric("ClaimContention", 1)
            return False

    claimed_ids = [t["ticketId"] for t in claimed]

    try:
        session_id = backends.active.provision()
    except Exception:
        claims.release_tickets(claimed_ids)
        metrics.emit_metric("ProvisionFailures", 1)
        raise

    if session_id is None:
        # No server to be had right now - a warm pool with nothing Ready.
        # Everyone goes back to waiting, and their next poll tries again.
        claims.release_tickets(claimed_ids)
        return False

    claims.stamp_session(claimed_ids, session_id)

    # Measured only once a server is in hand. Emitted at claim time, a claim
    # later released by a failed launch or an empty pool counted as a match
    # that never happened - once per poll for as long as the pool stayed empty.
    now = int(time.time())
    for ticket in claimed:
        metrics.emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")

    metrics.emit_metric("MatchesFormed", 1)
    metrics.emit_metric("ServersProvisioned", 1, sessionId = session_id)
    return True


def live_waiting_tickets(count):
    """Up to count waiting tickets whose players are still polling, oldest first.

    Liveness is the last poll, not the queue time. Filtering on queuedAt -
    the old approach - both matched ghosts who'd stopped polling seconds ago
    and permanently skipped real players who had simply waited a long time.

    No Limit on the query: DynamoDB applies Limit to items read *before* the
    filter, so Limit=2 with a ghost among the oldest two would hide the live
    tickets behind it. Paging until enough are found reads only what's needed.
    """
    cutoff = int(time.time()) - config.TICKET_STALE_SECONDS

    query = {
        "IndexName": "status-queuedAt-index",
        "KeyConditionExpression": Key("status").eq("waiting"),
        "FilterExpression": Attr("lastSeenAt").gte(cutoff),
        "ScanIndexForward": True,
    }

    found = []
    while True:
        page = ddb.queue.query(**query)
        found.extend(page.get("Items", []))

        if len(found) >= count or "LastEvaluatedKey" not in page:
            return found[:count]

        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def assign_tickets(tickets, session_id):
    """Claim what we can and point it at a session.

    Returns the tickets actually claimed - a claim can lose to a concurrent
    invocation, so this is not always everything that was passed in.
    """
    now = int(time.time())
    claimed_ids = []
    claimed_tickets = []

    for ticket in tickets:
        if claims.claim_ticket(ticket["ticketId"]):
            claimed_ids.append(ticket["ticketId"])
            claimed_tickets.append(ticket)
            metrics.emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")

    claims.stamp_session(claimed_ids, session_id)

    if claimed_tickets:
        metrics.emit_metric("MatchesFormed", 1)

    return claimed_tickets
