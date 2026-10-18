import json
import os
import sys
import time

# archive_file zips lambda/matchmaker wholesale, so bytecode written there by a
# local test run would ship in the next deploy. Must be set before the
# matchmaker modules are imported below.
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
    # Read when mm.backends.agones is imported. The files are never opened:
    # the agones fixture replaces the client before anything allocates.
    "AGONES_ALLOCATOR_URL": "https://allocator.test",
    "AGONES_CA_FILE": "/nonexistent/ca.crt",
    "AGONES_CLIENT_CERT_FILE": "/nonexistent/client.crt",
    "AGONES_CLIENT_KEY_FILE": "/nonexistent/client.key",
})

# Started before anything creates a boto3 client: the matchmaker builds its
# clients at import time, the same way it does on a Lambda cold start.
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


# --- AWS fakes -------------------------------------------------------------
#
# moto covers DynamoDB faithfully, but its ECS model crashes on awsvpc
# networking. These stand in for the two compute clients instead, which also
# makes the failure modes RunTask actually has easy to reproduce.

class FakeEcs:
    def __init__(self):
        self.run_calls = []
        self.raise_error = None
        self.placement_failure = None
        self.task_status = "RUNNING"

    def run_task(self, **kwargs):
        if self.raise_error:
            raise self.raise_error

        self.run_calls.append(kwargs)

        # RunTask's real failure shape: HTTP 200, empty tasks, reason in failures.
        if self.placement_failure:
            return {"tasks": [], "failures": [{"reason": self.placement_failure}]}

        arn = f"arn:aws:ecs:us-west-1:123456789012:task/test-cluster/task{len(self.run_calls)}"
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def describe_tasks(self, cluster, tasks):
        return {"tasks": [{
            "taskArn": tasks[0],
            "lastStatus": self.task_status,
            "attachments": [{
                "type": "eni",
                "details": [{"name": "networkInterfaceId", "value": "eni-test"}],
            }],
        }]}

    def session_ids_launched(self):
        ids = []
        for call in self.run_calls:
            for override in call["overrides"]["containerOverrides"]:
                for env in override.get("environment", []):
                    if env["name"] == "SESSION_ID":
                        ids.append(env["value"])
        return ids


class FakeEc2:
    def describe_network_interfaces(self, NetworkInterfaceIds):
        return {"NetworkInterfaces": [{"Association": {"PublicIp": PUBLIC_IP}}]}


@pytest.fixture(autouse=True)
def ecs(monkeypatch):
    fake = FakeEcs()
    monkeypatch.setattr(compute_module, "ecs", fake)
    monkeypatch.setattr(compute_module, "ec2", FakeEc2())
    return fake


def client_error(code, operation="RunTask"):
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


# --- Agones fake ------------------------------------------------------------
#
# Stands in for the allocator's REST endpoint. The response shapes are the
# ones a real Agones 1.61 allocator returned (docs/phase-2.md): 200 with the
# server's address, 429 when no server is Ready.

ALLOCATED_IP = "10.0.0.55"


class FakeAllocator:
    def __init__(self):
        self.requests = []
        self.status = 200
        self.raise_error = None
        self.next_port = 7000

    def allocate(self, request):
        if self.raise_error:
            raise self.raise_error

        self.requests.append(request)

        if self.status == 429:
            return 429, {"code": 8, "message": "there is no available GameServer to allocate", "details": []}
        if self.status == 409:
            return 409, {"code": 10, "message": "conflict in GameServer selection", "details": []}
        if self.status != 200:
            return self.status, {"message": "allocator broke"}

        self.next_port += 1
        return 200, {
            "gameServerName": f"ground-trace-abc12-gs{len(self.requests)}",
            "address": ALLOCATED_IP,
            "ports": [{"name": "default", "port": self.next_port}],
            "nodeName": "node-1",
        }


@pytest.fixture
def agones(monkeypatch):
    """Switch the matchmaker to the Agones backend, with a fake allocator."""
    from mm import backends
    from mm.backends import agones as agones_backend

    fake = FakeAllocator()
    monkeypatch.setattr(agones_backend, "allocator", fake)
    monkeypatch.setattr(backends, "active", agones_backend)
    return fake


# --- Tables ----------------------------------------------------------------
#
# Mirrors dynamodb.tf: the key schema and both GSIs are what the matchmaker's
# queries depend on.

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


def queue_table():
    return boto3.resource("dynamodb").Table("test-queue")


def sessions_table():
    return boto3.resource("dynamodb").Table("test-sessions")


def now():
    return int(time.time())


def put_ticket(ticket_id, queued_ago = 0, seen_ago = None, status = "waiting", **extra):
    queued_at = now() - queued_ago
    item = {
        "ticketId": ticket_id,
        "status": status,
        "queuedAt": queued_at,
        "expiresAt": queued_at + 300,
        **extra,
    }
    if seen_ago is not None:
        item["lastSeenAt"] = now() - seen_ago
    queue_table().put_item(Item = item)


def get_ticket(ticket_id):
    return queue_table().get_item(Key = {"ticketId": ticket_id}).get("Item")


def put_session(session_id, status = "waiting", heartbeat_ago = 0, players = 0,
                max_players = 2, ip = "203.0.113.5", port = 7777, orchestrator = None):
    item = {
        "sessionId": session_id,
        "status": status,
        "createdAt": now() - 60,
        "lastHeartbeat": now() - heartbeat_ago,
        "expiresAt": now() + 180,
        "playerCount": players,
        "maxPlayers": max_players,
        "port": port,
        "taskArn": f"arn:aws:ecs:us-west-1:123456789012:task/test-cluster/{session_id}",
    }
    if ip:
        item["publicIp"] = ip
    if orchestrator:
        item["orchestrator"] = orchestrator
    sessions_table().put_item(Item = item)


def get_session(session_id):
    return sessions_table().get_item(Key = {"sessionId": session_id}).get("Item")


def all_sessions():
    return sessions_table().scan().get("Items", [])


def fill_capacity():
    """Every slot under MAX_CONCURRENT_SESSIONS taken by a full, live match."""
    for i in range(int(os.environ["MAX_CONCURRENT_SESSIONS"])):
        put_session(f"full-{i}", status = "running", players = 2)


# --- API -------------------------------------------------------------------

def call(method, path, body = None, key = API_KEY, params = None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"x-api-key": key} if key is not None else {},
        "pathParameters": params or {},
        "body": json.dumps(body) if body is not None else None,
    }
    response = handler.lambda_handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def join():
    return call("POST", "/queue")


def poll(ticket_id):
    return call("GET", f"/queue/{ticket_id}", params = {"ticketId": ticket_id})


def leave(ticket_id):
    return call("DELETE", f"/queue/{ticket_id}", params = {"ticketId": ticket_id})


def heartbeat(session_id, body):
    return call("POST", f"/sessions/{session_id}/heartbeat", body = body,
                params = {"sessionId": session_id})


def emitted_metrics(capsys):
    """EMF lines printed to stdout, as {metric name: [payloads]}."""
    found = {}
    for line in capsys.readouterr().out.splitlines():
        if not line.startswith("{"):
            continue
        payload = json.loads(line)
        for metric in payload["_aws"]["CloudWatchMetrics"][0]["Metrics"]:
            found.setdefault(metric["Name"], []).append(payload)
    return found
