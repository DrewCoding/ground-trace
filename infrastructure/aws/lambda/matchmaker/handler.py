import os
import uuid
import time
import json
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
TICKET_TTL_SECONDS = 300
PLAYERS_PER_MATCH = 2

dynamodb = boto3.resource("dynamodb")
sessions = dynamodb.Table(SESSIONS_TABLE)
queue = dynamodb.Table(QUEUE_TABLE)
ecs = boto3.client("ecs")

def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["requestContext"]["http"]["path"]
    params = event.get("pathParameters") or {}

    try:
        if method == "POST" and path == "/queue":
            return respond(200, join_queue())

        if method == "GET" and path.startwith("/queue/"):
            return respond(200, poll_ticket(params["ticketId"]))

    except:
        return respond(400, None)

def join_queue():
    ticket_id = str(uuid.uuid4())
    now = int(time.time())

    queue.put_item(Item={
        "ticketId": ticket_id,
        "status": "waiting",
        "queuedAt": now,
        "expiresAt": now + TICKET_TTL_SECONDS,
    })

    return {"ticket_id": ticket_id, "status": "waiting"}

def try_form_match():
    cutoff = int(time.time()) - TICKET_TTL_SECONDS

    waiting = queue.query(
        IndexName = "status-queuedAt-index",
        KeyConditionExpression = Key("status").eq("waiting") & Key("queuedAt").gt(cutoff),
        Limit = PLAYERS_PER_MATCH,
        ScanIndexForward = True,
    ).get("Items", [])

    if len(waiting) < PLAYERS_PER_MATCH:
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

    for ticket_id in claimed:
        queue.update_item (
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET sessionId = :s",
            ExpressionAttributeValues = {":s": session_id},
        )

def claim_ticket(ticket_id):
    try:
        queue.update_item(
            Key = {"ticketId": ticket_id},
            UpdateExpression = "SET #s = :matched",
            ConditionExpression = "#s = waiting",
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
        pass

    return {
        "status": "matched",
        "sessionId": session_id,
        "ip": ip,
        "port": 7777
    }

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

    now = int(time.time())
    task_arn = task["tasks"][0]["taskArn"]

    sessions.put_items( Item = {
        "sessionId": session_id,
        "status": "provisioning",
        "createdAt": now,
        "lastHeartbeat": now,
        "expiredAt": now + 45 * 4,
        "playerCount": 0,
        "maxPlayers": PLAYERS_PER_MATCH,
        "port": 7777,
        "taskArn": task_arn,
    })

    return session_id

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