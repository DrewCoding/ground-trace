import logging
import os

SESSIONS_TABLE = os.environ["SESSIONS_TABLE"]
QUEUE_TABLE = os.environ["QUEUE_TABLE"]

PLAYERS_PER_MATCH = 2

# A ticket nobody has polled for this long is garbage. TTL deletes it
# eventually, but TTL makes no promptness guarantee, so poll_ticket checks the
# age itself rather than trusting the row's absence.
TICKET_TTL_SECONDS = 300

# A waiting ticket whose client has stopped polling - crashed, closed, lost
# its connection - is a ghost: matching it with a real player would launch a
# server for one person. Five missed polls at the client's 2s interval.
TICKET_STALE_SECONDS = 10

# How long a session can go without reporting before we stop offering it.
# TTL on the table is only garbage collection and AWS makes no promptness
# guarantee, so liveness is decided here by age instead. Comfortably longer
# than SessionHeartbeat's 10s interval.
SESSION_STALE_SECONDS = 45

# Garbage-collection horizon for a session row, refreshed by each heartbeat.
# Normally irrelevant: ecs_events deletes the row as soon as the task stops.
SESSION_EXPIRY_SECONDS = SESSION_STALE_SECONDS * 4

# A hard ceiling on how much this endpoint can ever cost. Unlike the shared
# secret, this holds even against someone who extracted the key from a game
# build - they can spam requests, but they cannot make us run more than this
# many Fargate tasks at once.
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "4"))

METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "GroundTrace/Matchmaker")

# Which compute backend launches game servers. See mm/backends.
ORCHESTRATOR = os.environ.get("ORCHESTRATOR", "fargate")

log = logging.getLogger()
log.setLevel(logging.INFO)
