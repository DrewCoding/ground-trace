import time
from mm import ddb, config
from boto3.dynamodb.conditions import Key

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
    return ddb.sessions.scan(Select="COUNT").get("Count", 0)

def heartbeat(session_id, body):
    now = int(time.time())

    ddb.sessions.update_item(
        Key = {"sessionId": session_id},
        UpdateExpression = (
            "SET #s = :status, playerCount = :count, "
            "lastHeartbeat = :now, expiresAt = :expires"
        ),
        ExpressionAttributeNames = {
            "#s" : "status"
        },
        ExpressionAttributeValues = {
            ":status": body.get("status", "waiting"),
            ":count": int(body.get("playerCount", 0)),
            ":now": now,
            ":expires": now + 45 * 4,
        }
    )

    return {"status": "ok"}