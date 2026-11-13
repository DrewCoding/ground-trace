"""Session rows: one per game server, from launch until its task stops."""
import time

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from mm import config, ddb
from mm.responses import NotFoundError

MAX_REPORTED_PLAYERS = 16


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


def holds_player(session, player_id, recently_matched, now):
    """Whether this account currently has a place in this session.

    Decided from what the server reports, never from what a client says: a
    client that could talk its way out of a match could queue for a second
    one. recently_matched covers the gap before the server can vouch for the
    player - while it boots, and until the heartbeat after they connect.
    """
    if not session or session.get("status") == "ending":
        return False

    # A cold start can outlast SESSION_STALE_SECONDS, and nothing heartbeats
    # until the server is up, so a booting session is judged by the grace
    # period alone.
    if session.get("status") == "provisioning":
        return recently_matched

    if int(session.get("lastHeartbeat", 0)) < now - config.SESSION_STALE_SECONDS:
        return False

    players = session.get("players") or []
    if player_id in players:
        return True

    # A full server that doesn't list them has no place left to hold. This is
    # the player who was matched to a slot someone else reached first: the
    # server turned them away, and without this they'd be handed the same
    # full server on every re-queue until the grace period ran out.
    if len(players) >= int(session.get("maxPlayers", config.PLAYERS_PER_MATCH)):
        return False

    return recently_matched


def heartbeat(session_id, body):
    now = int(time.time())

    # The accounts the server has connected, which is what lets holds_player
    # tell a player in a match from one who has left it. Only strings, and
    # bounded, since this is written straight into the row.
    reported = body.get("players")
    players = [
        p for p in (reported if isinstance(reported, list) else [])
        if isinstance(p, str) and len(p) <= 128
    ][:MAX_REPORTED_PLAYERS]

    # Conditional, because UpdateItem would otherwise create the row. Anyone
    # holding the game client's API key could then mint sessions out of
    # made-up ids, and since every row counts toward the server cap, a few
    # requests would stop all matchmaking.
    try:
        ddb.sessions.update_item(
            Key = {"sessionId": session_id},
            UpdateExpression = (
                "SET #s = :status, playerCount = :count, players = :players, "
                "lastHeartbeat = :now, expiresAt = :expires"
            ),
            ConditionExpression = ddb.EXISTS_SESSION,
            ExpressionAttributeNames = {
                "#s": "status"
            },
            ExpressionAttributeValues = {
                ":status": body.get("status", "waiting"),
                ":count": int(body.get("playerCount", 0)),
                ":players": players,
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
