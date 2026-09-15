import os
import uuid
import time
import json
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

QUEUE_TABLE = os.environ["QUEUE_TABLE"]

dynamodb = boto3.resource("dynamodb")
queue = dynamodb.Table(QUEUE_TABLE)

TICKET_TTL_SECONDS = 300
PLAYERS_PER_MATCH = 2

def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["requestContext"]["http"]["path"]
    params = event.get("pathParameters") or {}

    try:
        if method == "POST" and path == "/queue":
            return respond(200, join_queue())

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