"""The player-facing queue: join, poll, leave."""
import time
import uuid

from botocore.exceptions import ClientError

from mm import backends, config, ddb, jointoken, matching, metrics, sessions
from mm.responses import MatchmakerError


def join_queue(player_id):
    """Put this account in the queue - or return it to the place it already has.

    The row is keyed by the account, so a second ticket for the same player
    cannot exist however many clients they open. What a join changes is the
    attemptId, which is the "ticketId" a client is given and polls with:
    rotating it hands the place to the newest client, and every older one is
    told "replaced" on its next poll.

    An account already in a live match keeps that match. Queueing again from
    another window gets the same server back, never a second one.
    """
    now = int(time.time())
    attempt_id = str(uuid.uuid4())

    # Each write is conditional on the row still being what was just read.
    # Losing means another join for this account, or matching, got in between,
    # so read again. Three rounds is two more than this is ever likely to need.
    for _ in range(3):
        existing = _get(player_id)

        if existing and _holds_place(existing, player_id, now):
            if not _take_over(existing, attempt_id, now):
                continue

            metrics.emit_metric("DuplicateJoins", 1)

            if existing["status"] != "waiting":
                return {"ticketId": attempt_id, "status": "provisioning"}

            _match_opportunistically(new_ticket = {**existing, "lastSeenAt": now})
            return {"ticketId": attempt_id, "status": "waiting"}

        ticket = {
            "ticketId": player_id,
            "attemptId": attempt_id,
            "status": "waiting",
            "queuedAt": now,
            "lastSeenAt": now,
            "expiresAt": now + config.TICKET_TTL_SECONDS,
        }

        if not _create(ticket, replacing = existing):
            continue

        metrics.emit_metric("TicketsCreated", 1)
        _match_opportunistically(new_ticket = ticket)
        return {"ticketId": attempt_id, "status": "waiting"}

    raise MatchmakerError("could not join the queue, try again")


def poll_ticket(player_id, attempt_id):
    """Report a ticket's status - and, while it's waiting, try to match it.

    Clients poll every couple of seconds, so polls are the clock that drives
    matching. Nothing has to run on a timer, and nothing runs at all while
    nobody is waiting.
    """
    now = int(time.time())
    ticket = _get(player_id)

    if ticket and ticket.get("attemptId") != attempt_id:
        return {"status": "replaced"}

    if not _alive(ticket, now) or not _keep_alive(ticket, now):
        return {"status": "expired"}

    if ticket["status"] == "waiting":
        if not _match_opportunistically():
            return {"status": "waiting"}

        # Matching claimed something; it may have been this ticket.
        ticket = _get(player_id)
        if not ticket or ticket["status"] == "waiting":
            return {"status": "waiting"}

    return _describe_match(ticket)


def leave_queue(player_id, attempt_id):
    """Give up a place in the queue. A place in a match can't be given up here.

    Only a waiting ticket is deleted. If leaving also dropped a matched one,
    a client could leave and rejoin to queue for a second match while still
    connected to the first - the account is released by its server no longer
    reporting it, not by anything the client sends.
    """
    try:
        ddb.queue.delete_item(
            Key = {"ticketId": player_id},
            ConditionExpression = "attemptId = :attempt AND #s = :waiting",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {":attempt": attempt_id, ":waiting": "waiting"},
        )
        return {"status": "cancelled"}
    except ClientError as exc:
        if not ddb.condition_failed(exc):
            raise

    ticket = _get(player_id)
    if not ticket:
        return {"status": "cancelled"}
    if ticket.get("attemptId") != attempt_id:
        return {"status": "replaced"}
    return {"status": "matched"}


def _get(player_id):
    # Strongly consistent: a just-claimed ticket shouldn't read as waiting.
    return ddb.queue.get_item(
        Key = {"ticketId": player_id},
        ConsistentRead = True,
    ).get("Item")


def _holds_place(ticket, player_id, now):
    """Whether this row is a place worth keeping, rather than a leftover."""
    if ticket["status"] == "waiting":
        return _alive(ticket, now)

    recently_matched = now - int(ticket.get("matchedAt", 0)) <= config.MATCH_JOIN_GRACE_SECONDS

    session_id = ticket.get("sessionId")
    if not session_id:
        # Claimed, with the server not stamped on yet.
        return recently_matched

    session = ddb.sessions.get_item(Key = {"sessionId": session_id}).get("Item")
    return sessions.holds_player(session, player_id, recently_matched, now)


def _take_over(ticket, attempt_id, now):
    """Point an existing place at a new client. False if the row changed."""
    update = "SET attemptId = :new, lastSeenAt = :now"
    values = {":new": attempt_id, ":old": ticket.get("attemptId"), ":now": now}

    # A matched row already carries its long expiry from claim_ticket.
    if ticket["status"] == "waiting":
        update += ", expiresAt = :expires"
        values[":expires"] = now + config.TICKET_TTL_SECONDS

    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket["ticketId"]},
            UpdateExpression = update,
            # Also covers a status change: matching claiming the row doesn't
            # touch attemptId, but the caller branches on the status it read.
            ConditionExpression = "attemptId = :old AND #s = :status",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {**values, ":status": ticket["status"]},
        )
        return True
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return False
        raise


def _create(ticket, replacing):
    """Write a fresh ticket over nothing, or over the leftover just read.

    Conditional, because a blind put could land on a row another join for
    this account wrote a moment ago and matching has already claimed - which
    would wipe a place in a match its server is being started for.
    """
    condition = "attribute_not_exists(ticketId)"
    values = None

    if replacing:
        condition = "attemptId = :old AND #s = :status"
        values = {":old": replacing.get("attemptId"), ":status": replacing["status"]}

    put = {"Item": ticket, "ConditionExpression": condition}
    if values:
        put["ExpressionAttributeNames"] = {"#s": "status"}
        put["ExpressionAttributeValues"] = values

    try:
        ddb.queue.put_item(**put)
        return True
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return False
        raise


def _alive(ticket, now):
    """Present, and polled within TICKET_TTL_SECONDS.

    The row's existence alone isn't enough. TTL deletes expired items only
    "within a few days", and until then they read back like any other - so
    without this check an abandoned ticket would report "waiting" forever.
    """
    if not ticket:
        return False
    last_seen = int(ticket.get("lastSeenAt", ticket["queuedAt"]))
    return now - last_seen <= config.TICKET_TTL_SECONDS


def _keep_alive(ticket, now):
    """Record the poll. False if the ticket was cancelled or taken over since
    it was read.

    Pushing expiresAt forward on each poll means a player who is still
    waiting is never garbage collected, however long the wait - the client
    deliberately has no deadline for finding an opponent. A matched ticket
    keeps its long expiry instead: it has to outlast the match, and polling
    stops as soon as the player connects.
    """
    matched = ticket["status"] != "waiting"
    lifetime = config.MATCHED_TICKET_TTL_SECONDS if matched else config.TICKET_TTL_SECONDS

    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket["ticketId"]},
            UpdateExpression = "SET lastSeenAt = :now, expiresAt = :expires",
            ConditionExpression = "attemptId = :attempt",
            ExpressionAttributeValues = {
                ":now": now,
                ":expires": now + lifetime,
                ":attempt": ticket.get("attemptId"),
            },
        )
        return True
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return False
        raise


def _match_opportunistically(new_ticket = None):
    """Run matching without letting its failure fail the request.

    The request's own work - recording a ticket, reporting a status - is
    already done, and the next poll retries matching anyway. Failing here
    would only cost the player: a join that returned 503 left behind a ticket
    the client never learned the id of.

    The cause is logged with its traceback and counted, since the Lambda
    Errors alarm no longer sees it.
    """
    try:
        return matching.try_form_match(new_ticket)
    except Exception:
        config.log.exception("matching failed")
        metrics.emit_metric("MatchingErrors", 1)
        return False


def _describe_match(ticket):
    session_id = ticket.get("sessionId")
    if not session_id:
        return {"status": "provisioning"}

    session = ddb.sessions.get_item(Key = {"sessionId": session_id}).get("Item")
    if not session:
        return {"status": "expired"}

    address = backends.active.resolve_address(session)
    if not address:
        return {"status": "provisioning"}

    ip, port = address
    return {
        "status": "matched",
        "sessionId": session_id,
        "ip": ip,
        "port": port,
        # The address alone no longer gets anyone in: the server admits only
        # connections carrying one of these.
        "joinToken": jointoken.sign(ticket["ticketId"], session_id),
    }
