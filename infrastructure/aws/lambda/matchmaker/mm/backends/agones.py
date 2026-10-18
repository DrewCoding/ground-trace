"""A server from an Agones Fleet's warm pool, allocated when a match forms.

Unlike Fargate there's no cold start: the allocator hands back a server that
is already booted and Ready, address included, so a matched player never sees
"provisioning".

The allocator is called over REST with the standard library. gRPC would need
native dependencies packaged into the zip, and the Lambda's "boto3 only,
nothing to package" property is worth keeping.
"""
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from mm import config, metrics, sessions
from mm.responses import MatchmakerError

ALLOCATOR_URL = os.environ["AGONES_ALLOCATOR_URL"].rstrip("/")
NAMESPACE = os.environ.get("AGONES_NAMESPACE", "default")
FLEET = os.environ.get("AGONES_FLEET", "ground-trace")

# mTLS both ways. The CA is the one that signed the allocator's server
# certificate, not a public one: pinning it means nothing else can pose as the
# allocator. The client certificate must be on the allocator's allowlist
# (Helm's agones.allocator.clientCAs), or the TLS handshake itself fails.
CA_FILE = os.environ["AGONES_CA_FILE"]
CLIENT_CERT_FILE = os.environ["AGONES_CLIENT_CERT_FILE"]
CLIENT_KEY_FILE = os.environ["AGONES_CLIENT_KEY_FILE"]

# Every call happens inside a player's join or poll, behind API Gateway's 29s
# integration limit. An allocator that slow is broken, not busy.
TIMEOUT_SECONDS = 5


class AllocatorClient:
    def __init__(self):
        self._context = None

    def allocate(self, request):
        """POST a GameServerAllocation. Returns (HTTP status, parsed body).

        Non-2xx statuses are returned rather than raised: 429 and 409 are
        ordinary outcomes the caller decides about. Only failing to get an
        answer at all - unreachable, TLS refused, timed out - raises.
        """
        # Outside the try: a missing certificate file is a deploy mistake, and
        # reporting it as "allocator unreachable" would send someone chasing
        # the network.
        context = self._tls()

        req = urllib.request.Request(
            f"{ALLOCATOR_URL}/gameserverallocation",
            data = json.dumps(request).encode(),
            headers = {"Content-Type": "application/json"},
            method = "POST",
        )
        try:
            with urllib.request.urlopen(req, context = context, timeout = TIMEOUT_SECONDS) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"message": raw.decode(errors = "replace")}
        except OSError as exc:
            # URLError, TimeoutError and ssl.SSLError are all OSErrors. The
            # reason is the useful part: "certificate required" and
            # "connection refused" need very different fixes.
            raise MatchmakerError(f"allocator unreachable: {getattr(exc, 'reason', exc)}") from exc

    def _tls(self):
        # Built on first use rather than at import, so tests can swap the
        # client without certificate files on disk.
        #
        # Standard verification throughout - hostname checking and Python's
        # strict X.509 mode both stay on. Agones' Helm-generated server
        # certificate has no subject or SAN and fails both, which is why the
        # cluster is installed with our own certificate instead (see
        # docs/phase-2.md) rather than relaxing anything here.
        if self._context is None:
            context = ssl.create_default_context(cafile = CA_FILE)
            context.load_cert_chain(CLIENT_CERT_FILE, CLIENT_KEY_FILE)
            self._context = context
        return self._context


allocator = AllocatorClient()


def has_capacity():
    # The Fleet's size is the ceiling on this path: when the warm pool is
    # empty the allocator says so with a 429, which provision reports as
    # None. Counting session rows would be wrong here anyway - nothing deletes
    # them when an Agones server ends, so they'd fill any cap within a few
    # matches.
    return True


def provision():
    started = time.monotonic()
    status, body = allocator.allocate({
        "namespace": NAMESPACE,
        "gameServerSelectors": [{"matchLabels": {"agones.dev/fleet": FLEET}}],
    })
    elapsed = time.monotonic() - started

    # Both are answers, not failures: the REST allocator reports the
    # documented UnAllocated state as 429 (gRPC RESOURCE_EXHAUSTED) and
    # Contention as 409 (ABORTED), with no state field in the body.
    if status == 429:
        metrics.emit_metric("WarmPoolExhausted", 1)
        return None
    if status == 409:
        metrics.emit_metric("AllocationContention", 1)
        return None
    if status != 200:
        raise MatchmakerError(f"allocator returned HTTP {status}: {body.get('message', body)}")

    session_id = body["gameServerName"]
    now = int(time.time())

    # If this write fails, the server is allocated but nobody is sent to it.
    # That heals itself: the server's empty-shutdown timer starts at
    # allocation, so it exits and the Fleet replaces it.
    sessions.create({
        "sessionId": session_id,
        "orchestrator": "agones",
        # Not "waiting": find_joinable_session would offer it for backfill on
        # the strength of a playerCount nothing updates, because the Unity
        # server doesn't heartbeat under Agones. Backfill on this path belongs
        # to the allocator's own selectors instead.
        "status": "allocated",
        "createdAt": now,
        "lastHeartbeat": now,
        "expiresAt": now + config.SESSION_EXPIRY_SECONDS,
        "playerCount": 0,
        "maxPlayers": config.PLAYERS_PER_MATCH,
        "publicIp": body["address"],
        # A dynamic host port in 7000-8000, not the container's 7777.
        "port": int(body["ports"][0]["port"]),
    })

    # The Agones counterpart to ProvisionDurationSeconds: how long until the
    # player has an address. Here that's one round trip, not a cold start.
    metrics.emit_metric("AllocationDurationSeconds", round(elapsed, 3), "Seconds",
                        sessionId = session_id)

    return session_id


def resolve_address(session):
    # Known since allocation - there's nothing to look up.
    return session["publicIp"], int(session["port"])
