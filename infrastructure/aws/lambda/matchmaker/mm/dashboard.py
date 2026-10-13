import time

from mm import config, ddb


def dashboard_snapshot():
    """Everything the status page renders, in one request.

    Identifiers are truncated on purpose. A full sessionId is enough to spoof
    a heartbeat or deregister a session, and a full ticketId is enough to
    cancel someone else's place in the queue - so this returns only enough of
    each to tell rows apart visually. Public IPs are omitted entirely rather
    than advertising live game servers to anyone who loads the page.
    """
    now = int(time.time())

    live = []
    for item in ddb.sessions.scan().get("Items", []):
        heartbeat_age = now - int(item.get("lastHeartbeat", 0))
        live.append({
            "id": item["sessionId"][:8],
            "status": item.get("status", "unknown"),
            "playerCount": int(item.get("playerCount", 0)),
            "maxPlayers": int(item.get("maxPlayers", config.PLAYERS_PER_MATCH)),
            "ageSeconds": now - int(item.get("createdAt", now)),
            "heartbeatAgeSeconds": heartbeat_age,
            # Surfaces the distinction the matchmaker itself makes: a row can
            # exist while its server is already gone.
            "stale": heartbeat_age > config.SESSION_STALE_SECONDS,
            "ready": bool(item.get("publicIp")),
        })

    # Only the tickets matching would actually consider. A ghost whose client
    # stopped polling isn't really in the queue, whatever its row says.
    cutoff = now - config.TICKET_STALE_SECONDS
    waiting = []
    for item in ddb.queue.scan().get("Items", []):
        if item.get("status") != "waiting":
            continue
        if int(item.get("lastSeenAt", item.get("queuedAt", 0))) < cutoff:
            continue
        waiting.append({
            "id": item["ticketId"][:8],
            "waitingSeconds": now - int(item.get("queuedAt", now)),
        })

    return {
        "generatedAt": now,
        "sessions": sorted(live, key = lambda s: s["ageSeconds"]),
        "queue": sorted(waiting, key = lambda t: -t["waitingSeconds"]),
        "capacity": {
            "active": len(live),
            "max": config.MAX_CONCURRENT_SESSIONS,
            "playersPerMatch": config.PLAYERS_PER_MATCH,
        },
    }
