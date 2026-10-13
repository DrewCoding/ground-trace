"""The player-facing queue: join, poll, leave."""
import time
import uuid

from botocore.exceptions import ClientError

from mm import backends, config, ddb, matching, metrics


def join_queue():
    now = int(time.time())
    ticket = {
        "ticketId": str(uuid.uuid4()),
        "status": "waiting",
        "queuedAt": now,
        "lastSeenAt": now,
        "expiresAt": now + config.TICKET_TTL_SECONDS,
    }

    ddb.queue.put_item(Item = ticket)
    metrics.emit_metric("TicketsCreated", 1)

    _match_opportunistically(new_ticket = ticket)

    return {"ticketId": ticket["ticketId"], "status": "waiting"}


def poll_ticket(ticket_id):
    """Report a ticket's status - and, while it's waiting, try to match it.

    Clients poll every couple of seconds, so polls are the clock that drives
    matching. Nothing has to run on a timer, and nothing runs at all while
    nobody is waiting.
    """
    now = int(time.time())
    ticket = _get(ticket_id)

    if not _alive(ticket, now) or not _keep_alive(ticket_id, now):
        return {"status": "expired"}

    if ticket["status"] == "waiting":
        if not _match_opportunistically():
            return {"status": "waiting"}

        # Matching claimed something; it may have been this ticket.
        ticket = _get(ticket_id)
        if not ticket or ticket["status"] == "waiting":
            return {"status": "waiting"}

    return _describe_match(ticket)


def leave_queue(ticket_id):
    ddb.queue.delete_item(Key = {"ticketId": ticket_id})
    return {"status": "cancelled"}


def _get(ticket_id):
    # Strongly consistent: a just-claimed ticket shouldn't read as waiting.
    return ddb.queue.get_item(
        Key = {"ticketId": ticket_id},
        ConsistentRead = True,
    ).get("Item")


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


def _keep_alive(ticket_id, now):
    """Record the poll. False if the ticket was cancelled since it was read.

    Pushing expiresAt forward on each poll means a player who is still
    waiting is never garbage collected, however long the wait - the client
    deliberately has no deadline for finding an opponent.
    """
    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET lastSeenAt = :now, expiresAt = :expires",
            ConditionExpression = ddb.EXISTS_TICKET,
            ExpressionAttributeValues = {
                ":now": now,
                ":expires": now + config.TICKET_TTL_SECONDS,
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
    }
