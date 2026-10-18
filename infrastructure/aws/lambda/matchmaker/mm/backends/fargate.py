"""One Fargate task per session, launched when a match forms."""
import os
import time
import uuid

import boto3

from mm import config, metrics, sessions
from mm.responses import MatchmakerError

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
TASK_DEFINITION = os.environ["TASK_DEFINITION"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP_ID = os.environ["SECURITY_GROUP_ID"]
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "game-server")

# The port the container listens on, from the same Terraform variable as the
# task definition. Every task gets its own ENI, so it never needs to vary.
GAME_PORT = int(os.environ.get("GAME_PORT", "7777"))

ecs = boto3.client("ecs")
ec2 = boto3.client("ec2")


def has_capacity():
    return sessions.active_session_count() < config.MAX_CONCURRENT_SESSIONS


def provision():
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

    sessions.create({
        "sessionId": session_id,
        "orchestrator": "fargate",
        "status": "provisioning",
        "createdAt": now,
        "lastHeartbeat": now,
        "expiresAt": now + config.SESSION_EXPIRY_SECONDS,
        "playerCount": 0,
        "maxPlayers": config.PLAYERS_PER_MATCH,
        "port": GAME_PORT,
        "taskArn": task["tasks"][0]["taskArn"],
    })

    return session_id


def resolve_address(session):
    ip = session.get("publicIp") or _lookup_public_ip(session)
    if not ip:
        return None
    return ip, int(session.get("port", GAME_PORT))


def _lookup_public_ip(session):
    """A Fargate task can't discover its own public IP, so we look it up.

    The address lives on the task's ENI, outside the container. Driven by the
    client's own poll rather than a timer, so nothing runs while nobody waits.
    """
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
    if not ip:
        return None

    # The task may have stopped, and ecs_events removed its row, since the
    # poll read it. Handing out that address would send players to nothing.
    if not sessions.set_public_ip(session["sessionId"], ip):
        return None

    # The real cold-start number: RunTask to a reachable address. This is
    # the first poll that found an IP, so it overstates slightly by up to
    # one poll interval - but it's measured rather than guessed.
    metrics.emit_metric("ProvisionDurationSeconds",
                        int(time.time()) - int(session["createdAt"]), "Seconds",
                        sessionId = session["sessionId"])

    return ip
