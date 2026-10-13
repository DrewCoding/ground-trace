# Entry point for the matchmaker API: authentication, routing, and turning
# exceptions into responses. The work itself lives in the mm package.
import hmac
import json
import os
import traceback

from botocore.exceptions import ClientError

from mm import dashboard, sessions, tickets
from mm.config import log
from mm.responses import MatchmakerError, NotFoundError, respond

API_KEY = os.environ["API_KEY"]


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
        if not hmac.compare_digest(headers.get("x-api-key", ""), API_KEY):
            return respond(403, {"error": "forbidden"})

    try:
        if method == "GET" and path == "/dashboard":
            return respond(200, dashboard.dashboard_snapshot())

        if method == "POST" and path == "/queue":
            return respond(200, tickets.join_queue())

        if method == "GET" and path.startswith("/queue/"):
            return respond(200, tickets.poll_ticket(params["ticketId"]))

        if method == "DELETE" and path.startswith("/queue/"):
            return respond(200, tickets.leave_queue(params["ticketId"]))

        if method == "POST" and path.endswith("/heartbeat"):
            body = json.loads(event.get("body") or "{}")
            return respond(200, sessions.heartbeat(params["sessionId"], body))

        if method == "DELETE" and path.startswith("/sessions/"):
            return respond(200, sessions.deregister(params["sessionId"]))

        return respond(404, {"error": "no such route"})

    except KeyError as exc:
        return respond(400, {"error": f"missing {exc}"})

    except NotFoundError as exc:
        return respond(404, {"error": str(exc)})

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
