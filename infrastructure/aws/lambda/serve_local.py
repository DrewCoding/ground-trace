"""Run the matchmaker on this machine, against a local Agones cluster.

The full loop - Unity client, matchmaker, allocator, game server - with nothing
in AWS: lambda_handler served over plain HTTP, DynamoDB replaced by moto's
in-memory model, and ORCHESTRATOR=agones pointed at the k3s allocator. The
deployed Lambda can't reach a cluster on the LAN, and the allocator shouldn't
be exposed to the internet to make it (docs/phase-2.md, step 7).

    python serve_local.py                 # http://127.0.0.1:8080
    python serve_local.py --host 0.0.0.0  # reachable from other machines too

Needs requirements-dev.txt (moto). State lives in memory and is gone when
the process exits. Requests are handled one at a time, so this exercises the
whole flow but not the concurrent-claim races the tests cover.

Point the Unity client's MatchmakerClient at it: matchmakerUrl
http://127.0.0.1:8080 and apiKey matching --api-key. Unity 2022 refuses plain
http:// unless Player Settings > "Allow downloads over HTTP" is enabled.

Not shipped: archive_file only zips lambda/matchmaker.
"""
import argparse
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# Same reason as tests/conftest.py: bytecode written into lambda/matchmaker
# would ship in the next deploy.
sys.dont_write_bytecode = True

CERTS = os.path.expanduser("~/.ground-trace/allocator")

parser = argparse.ArgumentParser(description = __doc__.splitlines()[0])
parser.add_argument("--host", default = "127.0.0.1")
parser.add_argument("--port", type = int, default = 8080)
parser.add_argument("--api-key", default = "local-dev-key",
                    help = "what the Unity client must send as x-api-key")
parser.add_argument("--allocator", default = "https://10.0.0.55")
parser.add_argument("--fleet", default = "ground-trace")
args = parser.parse_args()

# Must all be set before the matchmaker is imported: it reads its settings and
# builds its AWS clients at import, the same way a Lambda cold start does.
os.environ.update({
    "AWS_DEFAULT_REGION": "us-west-1",
    "AWS_ACCESS_KEY_ID": "local",
    "AWS_SECRET_ACCESS_KEY": "local",
    "SESSIONS_TABLE": "local-sessions",
    "QUEUE_TABLE": "local-queue",
    "API_KEY": args.api_key,
    "ORCHESTRATOR": "agones",
    "AGONES_ALLOCATOR_URL": args.allocator,
    "AGONES_FLEET": args.fleet,
    "AGONES_CA_FILE": os.path.join(CERTS, "ca.crt"),
    "AGONES_CLIENT_CERT_FILE": os.path.join(CERTS, "client.crt"),
    "AGONES_CLIENT_KEY_FILE": os.path.join(CERTS, "client.key"),
})

for name in ("ca.crt", "client.crt", "client.key"):
    if not os.path.exists(os.path.join(CERTS, name)):
        sys.exit(f"missing {CERTS}/{name} - see docs/phase-2.md, step 1")

from moto import mock_aws

mock_aws().start()

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "matchmaker"))
import handler

# Mirrors dynamodb.tf: the key schema and both GSIs are what the matchmaker's
# queries depend on. TTL isn't modelled, which matches reality closely enough -
# AWS only promises deletion "within a few days".
TABLES = {
    "local-sessions": ("sessionId", "status-createdAt-index", "createdAt"),
    "local-queue": ("ticketId", "status-queuedAt-index", "queuedAt"),
}

_dynamodb = boto3.client("dynamodb")
for table, (key, index, range_key) in TABLES.items():
    _dynamodb.create_table(
        TableName = table,
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

# API Gateway extracts these from the route templates in apigateway.tf; the
# handler reads them from pathParameters rather than parsing the path.
ROUTES = [
    re.compile(r"^/queue/(?P<ticketId>[^/]+)$"),
    re.compile(r"^/sessions/(?P<sessionId>[^/]+)/heartbeat$"),
    re.compile(r"^/sessions/(?P<sessionId>[^/]+)$"),
]


def path_parameters(path):
    for pattern in ROUTES:
        match = pattern.match(path)
        if match:
            return match.groupdict()
    return {}


class ApiGateway(BaseHTTPRequestHandler):
    """Turns HTTP requests into the HTTP API v2 events lambda_handler expects."""

    def _invoke(self):
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else None

        event = {
            "requestContext": {"http": {"method": self.command, "path": path}},
            # HTTP API v2 lowercases header names, and the handler relies on it.
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "pathParameters": path_parameters(path),
            "body": body,
        }
        response = handler.lambda_handler(event, None)

        payload = response["body"].encode()
        self.send_response(response["statusCode"])
        for name, value in response.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _invoke

    def log_message(self, format, *log_args):
        # One line per request on stderr, so it stays apart from the EMF
        # metric lines the matchmaker prints to stdout.
        sys.stderr.write(f"{self.command} {self.path} -> {log_args[1]}\n")


if __name__ == "__main__":
    # The matchmaker's metrics are lines on stdout, which Python buffers when
    # it isn't a terminal - piped to a file, they'd show up minutes late.
    sys.stdout.reconfigure(line_buffering = True)
    print(f"matchmaker on http://{args.host}:{args.port}  "
          f"(api key {args.api_key!r}, allocator {args.allocator}, fleet {args.fleet})",
          file = sys.stderr)
    HTTPServer((args.host, args.port), ApiGateway).serve_forever()
