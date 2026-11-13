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


def _player_id(event):
    authorizer = event["requestContext"].get("authorizer") or {}
    return ((authorizer.get("jwt") or {}).get("claims") or {}).get("sub")


def lambda_handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["requestContext"]["http"]["path"]
    params = event.get("pathParameters") or {}

    # Three kinds of caller, three ways in. Which one applies is decided by
    # the route, and has to agree with the authorizers in apigateway.tf.
    #
    # /dashboard: operators. API Gateway has already checked a Cognito JWT
    # from the operator pool, so there is nothing left to check here.
    #
    # /queue: players. API Gateway has checked a JWT from the player pool and
    # passes its claims along; sub is the account, and the only identity the
    # queue trusts. Nothing in the request body or path can name a different
    # player. The claim is still required here, so that a route which lost
    # its authorizer in Terraform refuses everyone instead of admitting them.
    #
    # Everything else (/sessions): game servers, with the shared key. It no
    # longer ships in the client, so it is a secret again.
    #
    # HTTP API v2 lowercases header names, so this must be "x-api-key".
    # compare_digest rather than != so the comparison doesn't leak the key a
    # character at a time via timing.
    is_dashboard = method == "GET" and path == "/dashboard"
    is_queue = path == "/queue" or path.startswith("/queue/")
    player_id = None

    if is_queue:
        player_id = _player_id(event)
        if not player_id:
            return respond(401, {"error": "unauthorized"})
    elif not is_dashboard:
        headers = event.get("headers") or {}
        if not hmac.compare_digest(headers.get("x-api-key", ""), API_KEY):
            return respond(403, {"error": "forbidden"})

    try:
        if method == "GET" and path == "/dashboard":
            return respond(200, dashboard.dashboard_snapshot())

        if method == "POST" and path == "/queue":
            return respond(200, tickets.join_queue(player_id))

        if method == "GET" and path.startswith("/queue/"):
            return respond(200, tickets.poll_ticket(player_id, params["ticketId"]))

        if method == "DELETE" and path.startswith("/queue/"):
            return respond(200, tickets.leave_queue(player_id, params["ticketId"]))

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
