"""The Agones backend, driven through the API like the Fargate tests.

The allocator is faked with the response shapes a real Agones 1.61 allocator
returned when probed (docs/phase-2.md).
"""
import io
import json
import ssl
import urllib.error

import pytest

from mm.backends import agones as agones_backend
from mm.responses import MatchmakerError

from conftest import (
    ALLOCATED_IP, all_sessions, emitted_metrics, fill_capacity, get_session,
    get_ticket, heartbeat, join, poll, put_session,
)


def test_matched_players_get_an_address_without_waiting(agones, ecs):
    _, a = join()
    join()

    # The allocation already carried the address, so the very next poll is
    # "matched" - the provisioning wait is what this backend removes.
    status, body = poll(a["ticketId"])

    assert status == 200
    assert body == {"status": "matched", "sessionId": "ground-trace-abc12-gs1",
                    "ip": ALLOCATED_IP, "port": 7001}
    assert ecs.run_calls == []


def test_allocation_asks_the_fleet_for_a_server(agones):
    join()
    join()

    assert agones.requests == [{
        "namespace": "default",
        "gameServerSelectors": [{"matchLabels": {"agones.dev/fleet": "ground-trace"}}],
    }]


def test_session_row_is_tagged_and_not_joinable(agones):
    _, a = join()
    join()

    session = get_session(get_ticket(a["ticketId"])["sessionId"])

    assert session["orchestrator"] == "agones"
    assert session["status"] == "allocated"
    assert session["publicIp"] == ALLOCATED_IP


def test_empty_warm_pool_leaves_players_waiting(agones, capsys):
    agones.status = 429
    _, a = join()
    _, b = join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"
    assert all_sessions() == []

    metrics = emitted_metrics(capsys)
    assert "WarmPoolExhausted" in metrics
    # An empty pool is expected, not a fault.
    assert "ProvisionFailures" not in metrics
    assert "MatchingErrors" not in metrics
    # Released claims aren't matches.
    assert "TimeToMatchSeconds" not in metrics


def test_players_are_matched_once_a_server_is_ready(agones):
    agones.status = 429
    _, a = join()
    join()

    agones.status = 200
    _, body = poll(a["ticketId"])

    assert body["status"] == "matched"


def test_contention_is_retried_like_an_empty_pool(agones, capsys):
    agones.status = 409
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    metrics = emitted_metrics(capsys)
    assert "AllocationContention" in metrics
    assert "ProvisionFailures" not in metrics


def test_allocator_errors_release_players_and_are_counted(agones, capsys):
    agones.status = 500
    _, a = join()
    status, b = join()

    # The join itself still succeeds - matching retries on the next poll.
    assert status == 200
    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert get_ticket(b["ticketId"])["status"] == "waiting"
    assert "ProvisionFailures" in emitted_metrics(capsys)


def test_unreachable_allocator_releases_players(agones, capsys):
    agones.raise_error = MatchmakerError("allocator unreachable: connection refused")
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert "MatchingErrors" in emitted_metrics(capsys)


def test_fargate_rows_do_not_cap_agones(agones):
    # The Fleet's size bounds this path, not the session table.
    fill_capacity()
    _, a = join()
    join()

    assert get_ticket(a["ticketId"])["status"] == "matched"


def test_leftover_agones_rows_do_not_cap_fargate(ecs):
    # Nothing deletes Agones rows when their server ends; after switching
    # back to fargate they must not stop matchmaking until TTL gets to them.
    for i in range(4):
        put_session(f"gs-{i}", status = "allocated", orchestrator = "agones")

    join()
    join()

    assert len(ecs.run_calls) == 1


def test_lone_player_is_not_backfilled_before_the_server_reports(agones):
    join()
    join()
    _, c = join()

    # Until the server's first heartbeat, its playerCount is the matchmaker's
    # guess. Offering it would send a third player into what may be a full 1v1.
    _, body = poll(c["ticketId"])

    assert body == {"status": "waiting"}
    assert len(agones.requests) == 1


def test_lone_player_is_backfilled_once_the_server_reports_a_free_slot(agones):
    _, a = join()
    join()
    session_id = get_ticket(a["ticketId"])["sessionId"]

    # One player left; the server's heartbeat, sent under its GameServer
    # name, says so.
    status, _ = heartbeat(session_id, {"status": "waiting", "playerCount": 1})
    assert status == 200

    _, c = join()
    _, body = poll(c["ticketId"])

    # Through the same find_joinable_session path Fargate uses - no second
    # allocation.
    assert body["status"] == "matched"
    assert body["sessionId"] == session_id
    assert len(agones.requests) == 1


# --- Orchestrator dimension --------------------------------------------------

def test_metrics_keep_the_undimensioned_copy_the_dashboard_reads(ecs, capsys):
    join()
    join()

    payload = emitted_metrics(capsys)["MatchesFormed"][0]

    # [] is the metric dashboard.tf and the alarms query. Dropping it would
    # blank them; ["Orchestrator"] is the per-backend copy for Phase 6.
    assert payload["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[], ["Orchestrator"]]


def test_fargate_metrics_are_labelled_fargate(ecs, capsys):
    join()
    join()

    assert emitted_metrics(capsys)["MatchesFormed"][0]["Orchestrator"] == "fargate"


def test_agones_metrics_are_labelled_agones(agones, capsys):
    join()
    join()

    metrics = emitted_metrics(capsys)
    assert metrics["MatchesFormed"][0]["Orchestrator"] == "agones"
    assert metrics["AllocationDurationSeconds"][0]["Orchestrator"] == "agones"


# --- AllocatorClient: the real HTTP handling -------------------------------

def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://allocator.test/gameserverallocation", code, "error", {}, io.BytesIO(body))


@pytest.fixture
def client(monkeypatch):
    real = agones_backend.AllocatorClient()
    monkeypatch.setattr(real, "_tls", lambda: None)
    return real


def test_client_returns_error_statuses_instead_of_raising(client, monkeypatch):
    body = json.dumps({"code": 8, "message": "there is no available GameServer to allocate"}).encode()

    def urlopen(req, context, timeout):
        raise _http_error(429, body)

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    assert client.allocate({}) == (429, {"code": 8, "message": "there is no available GameServer to allocate"})


def test_client_tolerates_a_non_json_error_body(client, monkeypatch):
    def urlopen(req, context, timeout):
        raise _http_error(502, b"<html>bad gateway</html>")

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    assert client.allocate({}) == (502, {"message": "<html>bad gateway</html>"})


def test_client_reports_why_the_allocator_was_unreachable(client, monkeypatch):
    def urlopen(req, context, timeout):
        raise urllib.error.URLError("tlsv13 alert certificate required")

    monkeypatch.setattr(agones_backend.urllib.request, "urlopen", urlopen)

    with pytest.raises(MatchmakerError, match = "certificate required"):
        client.allocate({})


def test_missing_certificate_is_not_reported_as_unreachable():
    # The real _tls(), pointed at conftest's nonexistent paths.
    with pytest.raises(FileNotFoundError):
        agones_backend.AllocatorClient().allocate({})


# --- TLS material from SSM: how the deployed Lambda gets its certificates ---

TLS_PARAMETER = "/ground-trace/agones/allocator-tls"


def _issue(subject_cn, issuer_key=None, issuer_cert=None, ca=False, san=None, usage=None):
    """A certificate shaped to pass Python's strict X.509 mode, as ours are."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    signer = issuer_key or key
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_cert.subject if issuer_cert else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes = 1))
        .not_valid_after(now + datetime.timedelta(days = 1))
        .add_extension(x509.BasicConstraints(ca = ca, path_length = None), critical = True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical = False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(signer.public_key()), critical = False)
    )
    if ca:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature = False, content_commitment = False, key_encipherment = False,
            data_encipherment = False, key_agreement = False, key_cert_sign = True,
            crl_sign = True, encipher_only = False, decipher_only = False), critical = True)
    if san:
        builder = builder.add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address(san))]), critical = False)
    if usage:
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical = False)

    cert = builder.sign(signer, hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    return cert, key, cert_pem, key_pem


@pytest.fixture(scope = "module")
def pki():
    from cryptography.x509.oid import ExtendedKeyUsageOID

    ca, ca_key, ca_pem, _ = _issue("test-allocator-ca", ca = True)
    _, _, server_pem, server_key_pem = _issue(
        "allocator", ca_key, ca, san = "127.0.0.1", usage = ExtendedKeyUsageOID.SERVER_AUTH)
    _, _, client_pem, client_key_pem = _issue(
        "matchmaker", ca_key, ca, usage = ExtendedKeyUsageOID.CLIENT_AUTH)
    return {"ca": ca_pem, "server": server_pem, "server_key": server_key_pem,
            "client": client_pem, "client_key": client_key_pem}


@pytest.fixture
def tls_parameter(pki, monkeypatch):
    """The parameter as scripts/put-allocator-tls.sh writes it."""
    import boto3

    boto3.client("ssm").put_parameter(
        Name = TLS_PARAMETER, Type = "SecureString", Overwrite = True,
        Value = json.dumps({"ca": pki["ca"], "cert": pki["client"], "key": pki["client_key"]}))
    monkeypatch.setattr(agones_backend, "TLS_PARAMETER", TLS_PARAMETER)
    yield TLS_PARAMETER
    boto3.client("ssm").delete_parameter(Name = TLS_PARAMETER)


@pytest.fixture
def allocator_server(pki, tmp_path):
    """A real HTTPS endpoint that, like the allocator, demands a client cert
    from its CA and answers an empty pool's 429."""
    import http.server
    import threading

    for name in ("ca", "server", "server_key"):
        (tmp_path / name).write_text(pki[name])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            body = json.dumps({"code": 8, "message": "there is no available GameServer to allocate"}).encode()
            self.send_response(429)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server", tmp_path / "server_key")
    context.load_verify_locations(tmp_path / "ca")
    context.verify_mode = ssl.CERT_REQUIRED

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side = True)
    thread = threading.Thread(target = server.serve_forever, daemon = True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_certificates_from_ssm_complete_a_strict_mtls_handshake(tls_parameter, allocator_server, monkeypatch):
    monkeypatch.setattr(agones_backend, "ALLOCATOR_URL", allocator_server)
    client = agones_backend.AllocatorClient()

    # A real handshake: the server rejects a missing or foreign client cert,
    # and the client verifies the server with hostname checking and Python's
    # strict X.509 mode on - the checks Helm's generated certificate failed.
    status, body = client.allocate({})

    assert status == 429
    assert body["code"] == 8
    assert client._context.verify_flags & ssl.VERIFY_X509_STRICT
    assert client._context.check_hostname


def test_private_key_does_not_stay_on_disk(tls_parameter, tmp_path, monkeypatch):
    monkeypatch.setattr(agones_backend.tempfile, "tempdir", str(tmp_path))

    agones_backend.AllocatorClient()._tls()

    assert list(tmp_path.iterdir()) == []


def test_parameter_is_read_once_per_container(tls_parameter, monkeypatch):
    calls = []
    real_client = agones_backend.boto3.client

    def counting_client(service):
        calls.append(service)
        return real_client(service)

    monkeypatch.setattr(agones_backend.boto3, "client", counting_client)
    client = agones_backend.AllocatorClient()

    client._tls()
    client._tls()

    assert calls == ["ssm"]


def test_missing_parameter_leaves_players_waiting(agones, monkeypatch, capsys):
    # The real client this time, pointed at a parameter nobody uploaded -
    # what a window looks like if put-allocator-tls.sh was skipped.
    monkeypatch.setattr(agones_backend, "allocator", agones_backend.AllocatorClient())
    monkeypatch.setattr(agones_backend, "TLS_PARAMETER", "/ground-trace/agones/never-uploaded")

    _, a = join()
    status, _ = join()

    assert status == 200
    assert get_ticket(a["ticketId"])["status"] == "waiting"
    assert "MatchingErrors" in emitted_metrics(capsys)
