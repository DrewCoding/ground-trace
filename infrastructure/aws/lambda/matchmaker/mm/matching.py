import time
from boto3.dynamodb.conditions import Key
from mm.metrics import emit_metric
from mm import sessions, config, ddb, claims, backends

def try_form_match():
    cutoff = int(time.time()) - config.TICKET_TTL_SECONDS

    waiting = ddb.queue.query(
        IndexName = "status-queuedAt-index",
        KeyConditionExpression = Key("status").eq("waiting") & Key("queuedAt").gt(cutoff),
        Limit = config.PLAYERS_PER_MATCH,
        ScanIndexForward = True,
    ).get("Items", [])

    if not waiting:
        return

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
            emit_metric("SessionsBackfilled", 1,
                        sessionId = session["sessionId"], players = len(claimed))
        return

    # Nothing to join, so a new server has to be worth it: only provision
    # once there are enough players for a full match.
    if len(waiting) < config.PLAYERS_PER_MATCH:
        return

    if not backends.active.has_capacity():
        # Leave everyone queued rather than provisioning past the cap. They
        # keep polling and get matched once something frees up.
        emit_metric("CapacityCapHit", 1)
        return

    now = int(time.time())
    claimed = []
    for ticket in waiting:
        if claims.claim_ticket(ticket["ticketId"]):
            claimed.append(ticket["ticketId"])
            emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")
        else:
            claims.release_tickets(claimed)
            emit_metric("ClaimContention", 1)
            return

    try:
        session_id = backends.active.provision()
    except Exception:
        claims.release_tickets(claimed)
        emit_metric("ProvisionFailures", 1)
        raise

    claims.stamp_session(claimed, session_id)
    emit_metric("MatchesFormed", 1)
    emit_metric("ServersProvisioned", 1, sessionId = session_id)

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
            emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")

    claims.stamp_session(claimed_ids, session_id)

    if claimed_tickets:
        emit_metric("MatchesFormed", 1)

    return claimed_tickets