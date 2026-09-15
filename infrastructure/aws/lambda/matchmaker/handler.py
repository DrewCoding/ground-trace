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
        "expiresAt": now + 300,
    })

    return {"ticket_id": ticket_id, "status": "waiting"}

def respond(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=decimal_default)
    }

def decimal_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Not serializable: {type(value)}")