import os
import uuid
import time
import json
import hmac
import logging
import traceback
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

SESSIONS_TABLE = os.environ["SESSIONS_TABLE"]
QUEUE_TABLE = os.environ["QUEUE_TABLE"]
ECS_CLUSTER = os.environ["ECS_CLUSTER"]
TASK_DEFINITION = os.environ["TASK_DEFINITION"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "game-server")
API_KEY = os.environ["API_KEY"]
TICKET_TTL_SECONDS = 300
PLAYERS_PER_MATCH = 2

# How long a session can go without reporting before we stop offering it.
# TTL on the table is only garbage collection and AWS makes no promptness
# guarantee, so liveness is decided here by age instead. Comfortably longer
# than SessionHeartbeat's 10s interval.
SESSION_STALE_SECONDS = 45

# A hard ceiling on how much this endpoint can ever cost. Unlike the shared
# secret, this holds even against someone who extracted the key from a game
# build - they can spam requests, but they cannot make us run more than this
# many Fargate tasks at once.
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "4"))

log = logging.getLogger()
log.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
sessions = dynamodb.Table(SESSIONS_TABLE)
queue = dynamodb.Table(QUEUE_TABLE)
ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")

class MatchmakerError(Exception):
    """Something went wrong that the caller should hear a real reason for."""

def lambda_handler(event, context):
    # HTTP API v2 lowercases header names, so this must be "x-api-key" -
    # "X-Api-Key" would never match. compare_digest rather than != so the
    # comparison doesn't leak the key a character at a time via timing.
    headers = event.get("headers") or {}
    if not hmac.compare_digest(headers.get("x-api-key", ""), API_KEY):
        return respond(403, {"error": "forbidden"})

    method = event["requestContext"]["http"]["method"]
    path = event["requestContext"]["http"]["path"]
    params = event.get("pathParameters") or {}

    try:
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
        log.error("matchmaker: %s", exc)
        return respond(503, {"error": str(exc)})

    except ClientError as exc:
        # AWS refused something - almost always a missing IAM action or a
        # throttle. Surfacing the AWS error code in the response turns a
        # CloudWatch dig into a readable answer.
        code = exc.response["Error"]["Code"]
        log.error("aws %s on %s: %s", code, method, exc)
        return respond(502, {"error": "aws call failed", "code": code})

    except Exception as exc:
        # Anything unforeseen. Logged with a traceback so the cause is in
        # CloudWatch, but the client only gets a generic message - internal
        # details shouldn't leak out of a public endpoint.
        log.error("unhandled on %s %s: %s\n%s", method, path, exc, traceback.format_exc())
        return respond(500, {"error": "internal error"})

def join_queue():
    ticket_id = str(uuid.uuid4())
    now = int(time.time())

    queue.put_item(Item={
        "ticketId": ticket_id,
        "status": "waiting",
        "queuedAt": now,
        "expiresAt": now + TICKET_TTL_SECONDS,
    })

    try_form_match()

    return {"ticketId": ticket_id, "status": "waiting"}

def try_form_match():
    cutoff = int(time.time()) - TICKET_TTL_SECONDS

    waiting = queue.query(
        IndexName = "status-queuedAt-index",
        KeyConditionExpression = Key("status").eq("waiting") & Key("queuedAt").gt(cutoff),
        Limit = PLAYERS_PER_MATCH,
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
        free_slots = int(session.get("maxPlayers", PLAYERS_PER_MATCH)) - int(session.get("playerCount", 0))
        assign_tickets(waiting[:free_slots], session["sessionId"])
        return

    # Nothing to join, so a new server has to be worth it: only provision
    # once there are enough players for a full match.
    if len(waiting) < PLAYERS_PER_MATCH:
        return

    if active_session_count() >= MAX_CONCURRENT_SESSIONS:
        # Leave everyone queued rather than provisioning past the cap. They
        # keep polling and get matched once something frees up.
        return

    claimed = []
    for ticket in waiting:
        if claim_ticket(ticket["ticketId"]):
            claimed.append(ticket["ticketId"])
        else:
            release_tickets(claimed)
            return

    try:
        session_id = provision_session()
    except Exception:
        release_tickets(claimed)
        raise

    stamp_session(claimed, session_id)

def find_joinable_session():
    """An existing server that's up, reachable, and has room.

    The staleness check is the important part: a server that died without
    deregistering leaves its row behind until TTL gets around to it, and
    sending players to a dead address is worse than making them wait.
    """
    cutoff = int(time.time()) - SESSION_STALE_SECONDS

    candidates = sessions.query(
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

        if int(session.get("playerCount", 0)) < int(session.get("maxPlayers", PLAYERS_PER_MATCH)):
            return session

    return None

def assign_tickets(tickets, session_id):
    claimed = []
    for ticket in tickets:
        if claim_ticket(ticket["ticketId"]):
            claimed.append(ticket["ticketId"])

    stamp_session(claimed, session_id)

def stamp_session(ticket_ids, session_id):
    for ticket_id in ticket_ids:
        queue.update_item (
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
    return sessions.scan(Select="COUNT").get("Count", 0)

def claim_ticket(ticket_id):
    try:
        queue.update_item(
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
        queue.update_item (
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET #s = :waiting",
            ExpressionAttributeNames = {"#s": "status"},
            ExpressionAttributeValues = {":waiting": "waiting"},
        )

def poll_ticket(ticket_id):
    ticket = queue.get_item(Key = {"ticketId": ticket_id}).get("Item")
    if not ticket:
        return {"status": "expired"}

    if ticket["status"] == "waiting":
        return {"status": "waiting"}

    session_id = ticket.get("sessionId")
    if not session_id:
        return {"status": "provisioning"}

    session = sessions.get_item(Key = {"sessionId": session_id}).get("Item")
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
    queue.delete_item(Key = {"ticketId": ticket_id})
    return {"status": "cancelled"}

def provision_session():
    session_id = str(uuid.uuid4())

    task = ecs.run_task(
        cluster = ECS_CLUSTER,
        taskDefinition = TASK_DEFINITION,
        launchType = "FARGATE",
        count = 1,
        networkConfiguration = {
            "awsvpcConfiguration": {
                "subnets": SUBNET_IDS,
                "securityGroups": [SECURITY_GROUP_ID],
                "assignPublicIp": "ENABLED",
            }
        },
        overrides = {
            "containerOverrides": [{
                "name": CONTAINER_NAME,
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

    sessions.put_item( Item = {
        "sessionId": session_id,
        "status": "provisioning",
        "createdAt": now,
        "lastHeartbeat": now,
        "expiresAt": now + 45 * 4,
        "playerCount": 0,
        "maxPlayers": PLAYERS_PER_MATCH,
        "port": 7777,
        "taskArn": task_arn,
    })

    return session_id

def resolve_public_ip(session):
    task = ecs.describe_tasks(
        cluster = ECS_CLUSTER,
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
        sessions.update_item(
            Key = {"sessionId": session["sessionId"]},
            UpdateExpression = "SET publicIp = :ip",
            ExpressionAttributeValues = {":ip": ip},
        )

    return ip

def heartbeat(session_id, body):
    now = int(time.time())

    sessions.update_item(
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
    sessions.delete_item(Key = {"sessionId": session_id})
    return {"status": "deregistered"}

def respond(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=decimal_default),
    }

def decimal_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Not serializable: {type(value)}")