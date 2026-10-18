"""Session rows: one per game server, from launch until its task stops."""
import time

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from mm import config, ddb
from mm.responses import NotFoundError


def create(item):
    ddb.sessions.put_item(Item = item)


def find_joinable_session():
    """An existing server that's up, reachable, and has room.

    The staleness check is the important part: a server that died without
    deregistering leaves its row behind until TTL gets around to it, and
    sending players to a dead address is worse than making them wait.
    """
    cutoff = int(time.time()) - config.SESSION_STALE_SECONDS

    candidates = ddb.sessions.query(
        IndexName = "status-createdAt-index",
        KeyConditionExpression = Key("status").eq("waiting"),
        ScanIndexForward = True,
    ).get("Items", [])

    for session in candidates:
        if int(session.get("lastHeartbeat", 0)) < cutoff:
            continue

        if not session.get("publicIp"):
            # Still booting - no address to hand out yet.
            continue

        if int(session.get("playerCount", 0)) < int(session.get("maxPlayers", config.PLAYERS_PER_MATCH)):
            return session

    return None


def active_session_count():
    """How many Fargate game servers are being paid for right now.

    Every Fargate row is counted, live or not, and that is deliberate. A row
    exists from RunTask until ecs_events sees the task stop, which is exactly
    the span Fargate bills for - so a wedged server that stopped heartbeating
    still counts, because it's still costing money. Rows can't be created any
    other way: heartbeat refuses unknown sessions.

    Agones rows are excluded. Nothing deletes them when their server ends -
    ecs_events only hears about ECS tasks - so after switching ORCHESTRATOR
    back to fargate, a few leftover rows would fill the cap until TTL got to
    them, days later. Rows written before the orchestrator attribute existed
    are Fargate rows.

    A Scan reads the whole table, which is fine at this size and would need
    revisiting at volume - a counter item or a per-status query would scale,
    this won't.
    """
    return ddb.sessions.scan(
        Select = "COUNT",
        FilterExpression = Attr("orchestrator").not_exists() | Attr("orchestrator").eq("fargate"),
    ).get("Count", 0)


def heartbeat(session_id, body):
    now = int(time.time())

    # Conditional, because UpdateItem would otherwise create the row. Anyone
    # holding the game client's API key could then mint sessions out of
    # made-up ids, and since every row counts toward the server cap, a few
    # requests would stop all matchmaking.
    try:
        ddb.sessions.update_item(
            Key = {"sessionId": session_id},
            UpdateExpression = (
                "SET #s = :status, playerCount = :count, "
                "lastHeartbeat = :now, expiresAt = :expires"
            ),
            ConditionExpression = ddb.EXISTS_SESSION,
            ExpressionAttributeNames = {
                "#s": "status"
            },
            ExpressionAttributeValues = {
                ":status": body.get("status", "waiting"),
                ":count": int(body.get("playerCount", 0)),
                ":now": now,
                ":expires": now + config.SESSION_EXPIRY_SECONDS,
            },
        )
    except ClientError as exc:
        if ddb.condition_failed(exc):
            raise NotFoundError("unknown session")
        raise

    return {"status": "ok"}


def deregister(session_id):
    ddb.sessions.delete_item(Key = {"sessionId": session_id})
    return {"status": "deregistered"}


def set_public_ip(session_id, ip):
    """Cache a resolved address. False if the session has already ended."""
    try:
        ddb.sessions.update_item(
            Key = {"sessionId": session_id},
            UpdateExpression = "SET publicIp = :ip",
            ConditionExpression = ddb.EXISTS_SESSION,
            ExpressionAttributeValues = {":ip": ip},
        )
        return True
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return False
        raise


def end_session(session_id):
    """Delete a session's row. Returns the row, or None if it was already gone.

    Only one caller can get the row back, which makes this the deduplication
    point for anything that must happen exactly once per session.
    """
    try:
        return ddb.sessions.delete_item(
            Key = {"sessionId": session_id},
            ConditionExpression = ddb.EXISTS_SESSION,
            ReturnValues = "ALL_OLD",
        ).get("Attributes")
    except ClientError as exc:
        if ddb.condition_failed(exc):
            return None
        raise
