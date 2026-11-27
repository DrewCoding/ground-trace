import hmac
import json
import time
import traceback
import uuid

from botocore.exceptions import ClientError

from mm import backends, config, ddb, sessions, matching
from mm.dashboard import dashboard_snapshot
from mm.metrics import emit_metric
from mm.responses import MatchmakerError, respond

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
            return respond(200, sessions.heartbeat(params["sessionId"], body))

        if method == "DELETE" and path.startswith("/sessions/"):
            return respond(200, sessions.deregister(params["sessionId"]))

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

    matching.try_form_match()

    return {"ticketId": ticket_id, "status": "waiting"}

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

    address = backends.active.resolve_address(session)
    if not address:
        return {"status": "provisioning"}

    ip, port = address
    return {
        "status": "matched",
        "sessionId": session_id,
        "ip": ip,
        "port": port,
    }

def leave_queue(ticket_id):
    ddb.queue.delete_item(Key = {"ticketId": ticket_id})
    return {"status": "cancelled"}
