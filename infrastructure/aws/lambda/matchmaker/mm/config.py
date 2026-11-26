import logging
import os

SESSIONS_TABLE = os.environ["SESSIONS_TABLE"]
QUEUE_TABLE = os.environ["QUEUE_TABLE"]
API_KEY = os.environ["API_KEY"]
TICKET_TTL_SECONDS = 300
PLAYERS_PER_MATCH = 2

# How long a session can go without reporting before we stop offering it.
# TTL on the table is only garbage collection and AWS makes no promptness
# guarantee, so liveness is decided here by age instead. Comfortably longer
# than SessionHeartbeat's 10s interval.
SESSION_STALE_SECONDS = 45

# A hard ceiling on how much this endpoint can ever cost. Unlike the shared
# secret, this holds even against someone who extracted the key from a game
# build - they can spam requests, but they cannot make us run more than this
# many Fargate tasks at once.
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "4"))

METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "GroundTrace/Matchmaker")

log = logging.getLogger()
log.setLevel(logging.INFO)
