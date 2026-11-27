import time

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from mm import config, ddb
from mm.responses import NotFoundError


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
    """How many sessions exist right now, in any state.

    A Scan reads the whole table, which is fine at this size and would need
    revisiting at volume - a counter item or a per-status query would scale,
    this won't.
    """
    return ddb.sessions.scan(Select = "COUNT").get("Count", 0)


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
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {
                ":status": body.get("status", "waiting"),
                ":count": int(body.get("playerCount", 0)),
                ":now": now,
                ":expires": now + 45 * 4,
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
    """Cache a resolved address. False if the session has already ended.

    The caller read the row a moment ago, but the server can stop in between.
    Writing unconditionally would bring back a row with an address and no
    heartbeat - a session that counts against the cap until TTL removes it.
    """
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
