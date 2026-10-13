"""Where game servers come from.

A backend is a module with two functions:

    provision() -> session_id
        Start (or claim) a server and create its session row. May return
        before the server has an address.

    resolve_address(session) -> (ip, port) | None
        Where players should connect, or None while it isn't known yet.

Everything else - queueing, claiming, backfill, the server cap - is shared,
which is what makes the backends comparable.
"""
from mm import config
from mm.backends import fargate

_BACKENDS = {
    "fargate": fargate,
}

# Resolved at import so a typo in ORCHESTRATOR fails the cold start loudly,
# instead of the first request that happens to need a server.
if config.ORCHESTRATOR not in _BACKENDS:
    raise RuntimeError(
        f"unknown ORCHESTRATOR {config.ORCHESTRATOR!r}; expected one of {sorted(_BACKENDS)}"
    )

active = _BACKENDS[config.ORCHESTRATOR]
