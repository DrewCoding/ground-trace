import uuid
import time
import json
import hmac
import traceback
from mm.responses import respond, MatchmakerError 
from mm.metrics import emit_metric

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from mm import config
from mm import ddb

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")

def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["requestContext"]["http"]["path"]
    params = event.get("pathParameters") or {}

    # The dashboard route is exempt from the shared key because API Gateway
    # has already authenticated it with a Cognito JWT - the request arrives
    # with a Bearer token and no x-api-key, so requiring one here would reject
    # every legitimate request. Not an unauthenticated path: an invalid token
    # is refused by the authorizer before this function is ever invoked.
    #
    # Identifiers in the response are still truncated, because the key the
    # game client carries is recoverable and shouldn't be the only thing
    # standing between a leak and an actionable session id.
    #
    # HTTP API v2 lowercases header names, so this must be "x-api-key".
    # compare_digest rather than != so the comparison doesn't leak the key a
    # character at a time via timing.
    if not (method == "GET" and path == "/dashboard"):
        headers = event.get("headers") or {}
        if not hmac.compare_digest(headers.get("x-api-key", ""), config.API_KEY):
            return respond(403, {"error": "forbidden"})

    try:
        if method == "GET" and path == "/dashboard":
            return respond(200, dashboard_snapshot())

        if method == "POST" and path == "/queue":
            return respond(200, join_queue())

        if method == "GET" and path.startswith("/queue/"):
            return respond(200, poll_ticket(params["ticketId"]))

        if method == "DELETE" and path.startswith("/queue/"):
            return respond(200, leave_queue(params["ticketId"]))

        if method == "POST" and path.endswith("/heartbeat"):
            body = json.loads(event.get("body") or "{}")
            return respond(200, heartbeat(params["sessionId"], body))

        if method == "DELETE" and path.startswith("/sessions/"):
            return respond(200, deregister(params["sessionId"]))

        return respond(404, {"error": "no such route"})

    except KeyError as exc:
        return respond(400, {"error": f"missing {exc}"})

    except MatchmakerError as exc:
        config.log.error("matchmaker: %s", exc)
        return respond(503, {"error": str(exc)})

    except ClientError as exc:
        # AWS refused something - almost always a missing IAM action or a
        # throttle. Surfacing the AWS error code in the response turns a
        # CloudWatch dig into a readable answer.
        code = exc.response["Error"]["Code"]
        config.log.error("aws %s on %s: %s", code, method, exc)
        return respond(502, {"error": "aws call failed", "code": code})

    except Exception as exc:
        # Anything unforeseen. Logged with a traceback so the cause is in
        # CloudWatch, but the client only gets a generic message - internal
        # details shouldn't leak out of a public endpoint.
        config.log.error("unhandled on %s %s: %s\n%s", method, path, exc, traceback.format_exc())
        return respond(500, {"error": "internal error"})

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

    waiting = []
    for item in ddb.queue.scan().get("Items", []):
        if item.get("status") != "waiting":
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

def join_queue():
    ticket_id = str(uuid.uuid4())
    now = int(time.time())

    ddb.queue.put_item(Item={
        "ticketId": ticket_id,
        "status": "waiting",
        "queuedAt": now,
        "expiresAt": now + config.TICKET_TTL_SECONDS,
    })

    emit_metric("TicketsCreated", 1)

    try_form_match()

    return {"ticketId": ticket_id, "status": "waiting"}

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
    session = find_joinable_session()
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

    if active_session_count() >= config.MAX_CONCURRENT_SESSIONS:
        # Leave everyone queued rather than provisioning past the cap. They
        # keep polling and get matched once something frees up.
        emit_metric("CapacityCapHit", 1)
        return

    now = int(time.time())
    claimed = []
    for ticket in waiting:
        if claim_ticket(ticket["ticketId"]):
            claimed.append(ticket["ticketId"])
            emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")
        else:
            release_tickets(claimed)
            emit_metric("ClaimContention", 1)
            return

    try:
        session_id = provision_session()
    except Exception:
        release_tickets(claimed)
        emit_metric("ProvisionFailures", 1)
        raise

    stamp_session(claimed, session_id)
    emit_metric("MatchesFormed", 1)
    emit_metric("ServersProvisioned", 1, sessionId = session_id)

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

def assign_tickets(tickets, session_id):
    """Claim what we can and point it at a session.

    Returns the tickets actually claimed - a claim can lose to a concurrent
    invocation, so this is not always everything that was passed in.
    """
    now = int(time.time())
    claimed_ids = []
    claimed_tickets = []

    for ticket in tickets:
        if claim_ticket(ticket["ticketId"]):
            claimed_ids.append(ticket["ticketId"])
            claimed_tickets.append(ticket)
            emit_metric("TimeToMatchSeconds", now - int(ticket["queuedAt"]), "Seconds")

    stamp_session(claimed_ids, session_id)

    if claimed_tickets:
        emit_metric("MatchesFormed", 1)

    return claimed_tickets

def stamp_session(ticket_ids, session_id):
    for ticket_id in ticket_ids:
        ddb.queue.update_item (
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET sessionId = :s",
            ExpressionAttributeValues = {":s": session_id},
        )

def active_session_count():
    """How many sessions exist right now, in any state.

    A Scan reads the whole table, which is fine at this size and would need
    revisiting at volume - a counter item or a per-status query would scale,
    this won't.
    """
    return ddb.sessions.scan(Select="COUNT").get("Count", 0)

def claim_ticket(ticket_id):
    try:
        ddb.queue.update_item(
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET #s = :matched",
            ConditionExpression = "#s = :waiting",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {":matched": "matched", ":waiting": "waiting"},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise

def release_tickets(ticket_ids):
    for ticket_id in ticket_ids:
        ddb.queue.update_item (
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET #s = :waiting",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {":waiting": "waiting"},
        )

def poll_ticket(ticket_id):
    ticket = ddb.queue.get_item(Key = {"ticketId": ticket_id}).get("Item")
    if not ticket:
        return {"status": "expired"}

    if ticket["status"] == "waiting":
        return {"status": "waiting"}

    session_id = ticket.get("sessionId")
    if not session_id:
        return {"status": "provisioning"}

    session = ddb.sessions.get_item(Key = {"sessionId": session_id}).get("Item")
    if not session:
        return {"status": "expired"}

    ip = session.get("publicIp")

    if not ip:
        ip = resolve_public_ip(session)
    
    if not ip:
        return {"status": "provisioning"}

    return {
        "status": "matched",
        "sessionId": session_id,
        "ip": ip,
        "port": 7777
    }

def leave_queue(ticket_id):
    ddb.queue.delete_item(Key = {"ticketId": ticket_id})
    return {"status": "cancelled"}

def provision_session():
    session_id = str(uuid.uuid4())

    task = ecs.run_task(
        cluster = config.ECS_CLUSTER,
        taskDefinition = config.TASK_DEFINITION,
        launchType = "FARGATE",
        count = 1,
        networkConfiguration = {
            "awsvpcConfiguration": {
                "subnets": config.SUBNET_IDS,
                "securityGroups": [config.SECURITY_GROUP_ID],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides = {
            "containerOverrides": [{
                "name": config.CONTAINER_NAME,
                "environment": [{"name": "SESSION_ID", "value": session_id}],
            }]
        },
    )

    # RunTask returns HTTP 200 even when it couldn't place the task: the
    # reason goes into "failures" and "tasks" comes back empty. Indexing
    # straight into tasks[0] would raise IndexError and throw away the only
    # useful information. Common real causes are no free IPs left in the
    # subnet, Fargate capacity shortfalls, and account task quotas.
    failures = task.get("failures") or []
    if failures or not task.get("tasks"):
        reason = failures[0].get("reason", "unknown") if failures else "no task returned"
        raise MatchmakerError(f"could not start game server: {reason}")

    now = int(time.time())
    task_arn = task["tasks"][0]["taskArn"]

    ddb.sessions.put_item( Item = {
        "sessionId": session_id,
        "status": "provisioning",
        "createdAt": now,
        "lastHeartbeat": now,
        "expiresAt": now + 45 * 4,
        "playerCount": 0,
        "maxPlayers": config.PLAYERS_PER_MATCH,
        "port": 7777,
        "taskArn": task_arn,
    })

    return session_id

def resolve_public_ip(session):
    task = ecs.describe_tasks(
        cluster = config.ECS_CLUSTER,
        tasks = [session["taskArn"]],
    ).get("tasks", [])

    if not task or task[0]["lastStatus"] != "RUNNING":
        return None

    eni_id = None
    for attachment in task[0].get("attachments", []):
        for detail in attachment.get("details", []):
            if detail["name"] == "networkInterfaceId":
                eni_id = detail["value"]

    if not eni_id:
        return None

    enis = ec2.describe_network_interfaces(NetworkInterfaceIds = [eni_id])
    associate = enis["NetworkInterfaces"][0].get("Association") or {}
    ip = associate.get("PublicIp")

    if ip:
        ddb.sessions.update_item(
            Key = {"sessionId": session["sessionId"]},
            UpdateExpression = "SET publicIp = :ip",
            ExpressionAttributeValues = {":ip": ip},
        )

        # The real cold-start number: RunTask to a reachable address. This is
        # the first poll that found an IP, so it overstates slightly by up to
        # one poll interval - but it's measured rather than guessed.
        emit_metric("ProvisionDurationSeconds",
                    int(time.time()) - int(session["createdAt"]), "Seconds",
                    sessionId = session["sessionId"])

    return ip

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

def deregister(session_id):
    ddb.sessions.delete_item(Key = {"sessionId": session_id})
    return {"status": "deregistered"}
