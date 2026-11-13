"""Ticket state transitions shared by matching and the ticket API.

Kept apart from tickets.py so both it and matching.py can use them without
importing each other.
"""
import time

from botocore.exceptions import ClientError

from mm import config, ddb


def claim_ticket(ticket_id):
    """Move a ticket from waiting to matched, if nobody else got there first.

    The conditional write is the whole concurrency story: two invocations
    racing for the same ticket can't both succeed, so no lock is needed.

    matchedAt starts the grace period in which the account counts as in the
    match before its server has reported it, and the long expiresAt keeps the
    row for the length of the match - see tickets.join_queue.
    """
    now = int(time.time())
    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET #s = :matched, matchedAt = :now, expiresAt = :expires",
            ConditionExpression = "#s = :waiting",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {
                ":matched": "matched",
                ":waiting": "waiting",
                ":now": now,
                ":expires": now + config.MATCHED_TICKET_TTL_SECONDS,
            },
        )
        return True
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return False
        raise


def release_tickets(ticket_ids):
    # Undoes everything claim_ticket set, expiry included: left with a
    # matched ticket's day-long expiresAt, an abandoned waiting row would
    # outlive its player by that long.
    expires = int(time.time()) + config.TICKET_TTL_SECONDS
    for ticket_id in ticket_ids:
        _update_if_present(
            ticket_id,
            UpdateExpression = "SET #s = :waiting, expiresAt = :expires REMOVE matchedAt",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {":waiting": "waiting", ":expires": expires},
        )


def stamp_session(ticket_ids, session_id):
    for ticket_id in ticket_ids:
        _update_if_present(
            ticket_id,
            UpdateExpression = "SET sessionId = :s",
            ExpressionAttributeValues = {":s": session_id},
        )


def _update_if_present(ticket_id, **update):
    # A ticket can be cancelled between being claimed and being stamped or
    # released. That player is gone, so there's nothing left to do for them.
    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket_id},
            ConditionExpression = ddb.EXISTS_TICKET,
            **update,
        )
    except ClientError as exc:
        if not ddb.condition_failed(exc):
            raise
