import json
import os
import sys

sys.dont_write_bytecode = True

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "matchmaker"))

os.environ.update({
    "AWS_DEFAULT_REGION": "us-west-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "SESSIONS_TABLE": "test-sessions",
    "QUEUE_TABLE": "test-queue",
    "ECS_CLUSTER": "test-cluster",
    "TASK_DEFINITION": "test-game-server",
    "SUBNET_IDS": "subnet-a,subnet-b",
    "SECURITY_GROUP_ID": "sg-test",
    "API_KEY": "test-key",
    "MAX_CONCURRENT_SESSIONS": "4",
})

from moto import mock_aws

_aws = mock_aws()
_aws.start()

import boto3
import pytest
from botocore.exceptions import ClientError

import handler

try:
    import mm.backends.fargate as compute_module
except ImportError:
    # The pre-refactor single-file handler, which held its clients itself.
    compute_module = handler

API_KEY = os.environ["API_KEY"]
PUBLIC_IP = "203.0.113.10"

TABLES = {
    "test-sessions": ("sessionId", "status-createdAt-index", "createdAt"),
    "test-queue": ("ticketId", "status-queuedAt-index", "queuedAt"),
}


@pytest.fixture(autouse=True)
def tables():
    client = boto3.client("dynamodb")

    for name, (key, index, range_key) in TABLES.items():
        client.create_table(
            TableName = name,
            BillingMode = "PAY_PER_REQUEST",
            KeySchema = [{"AttributeName": key, "KeyType": "HASH"}],
            AttributeDefinitions = [
                {"AttributeName": key, "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": range_key, "AttributeType": "N"},
            ],
            GlobalSecondaryIndexes = [{
                "IndexName": index,
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": range_key, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )

    yield

    for name in TABLES:
        try:
            client.delete_table(TableName = name)
        except client.exceptions.ResourceNotFoundException:
            pass    # a test removed it deliberately

def call(method, path, body = None, key = API_KEY, params = None, player = None):
    context = {"http": {"method": method, "path": path}}
    if player is not None:
        # What API Gateway's JWT authorizer adds once it has verified a token.
        context["authorizer"] = {"jwt": {"claims": {"sub": player}}}

    event = {
        "requestContext": context,
        "headers": {"x-api-key": key} if key is not None else {},
        "pathParameters": params or {},
        "body": json.dumps(body) if body is not None else None,
    }
    response = handler.lambda_handler(event, None)
    return response["statusCode"], json.loads(response["body"])