"""Where game servers come from.

A backend is a module with three functions:

    has_capacity() -> bool
        Whether launching another server is allowed right now. The cost
        ceiling lives here because each backend bounds spend differently.

    provision() -> session_id | None
        Start (Fargate) or claim (Agones) a server and create its session
        row. May return before the server has an address. None means no
        server can be had right now - an expected outcome, not an error - and
        matching releases the players to try again on their next poll.
        Raises MatchmakerError when something is actually broken.

    resolve_address(session) -> (ip, port) | None
        Where players should connect, or None while it isn't known yet.

Everything else (queueing, claiming, backfill) is shared, which is what
makes backends interchangeable.

Callers always go through the module attribute - backends.active.provision(),
never `from mm.backends import active` - so tests, and anything else, can
swap the backend at runtime.
"""
import importlib

from mm import config

_BACKENDS = ("fargate", "agones")

# Checked at import so a typo in ORCHESTRATOR fails the cold start loudly,
# instead of the first request that happens to need a server.
if config.ORCHESTRATOR not in _BACKENDS:
    raise RuntimeError(
        f"unknown ORCHESTRATOR {config.ORCHESTRATOR!r}; expected one of {sorted(_BACKENDS)}"
    )

# Only the selected backend is imported. Each reads its own required settings
# at import, so loading both would make a Fargate deploy need Agones
# certificates, and an Agones one need ECS subnets.
active = importlib.import_module(f"mm.backends.{config.ORCHESTRATOR}")
