"""Where game servers come from.

A backend is a module with three functions:

    has_capacity() -> bool
        Whether launching another server is allowed right now. The cost
        ceiling lives here because each backend bounds spend differently.

    provision() -> session_id
        Start a server and create its session row. Returns before the
        server has an address, because a cold start outlasts API Gateway's
        29-second limit. Raises MatchmakerError if no server could be
        started.

    resolve_address(session) -> (ip, port) | None
        Where players should connect, or None while it isn't known yet.

Everything else (queueing, claiming, backfill) is shared, which is what
makes backends interchangeable.
"""
from mm.backends import fargate as active
