import boto3

from conftest import (
    call
)


def test_session_routes_reject_requests_without_the_api_key():

    for key in (None, "wrong"):
        status, _ = call("POST", "/sessions/s1/heartbeat", body = {}, key = key,
                         params = {"sessionId": "s1"})
        assert status == 403


def test_dashboard_is_exempt_from_the_api_key():
    # API Gateway's JWT authorizer has already vetted these requests.
    status, body = call("GET", "/dashboard", key = None)

    assert status == 200
    assert body["capacity"]["max"] == 4


def test_unknown_route_is_404():
    assert call("GET", "/nope")[0] == 404


