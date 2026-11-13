"""Proof, for the game server, that the matchmaker sent this player.

Without it the server has to admit anyone who reaches its address, logged in
or not. The token is payload.signature, both base64url: the payload is JSON
naming the account, the session and an expiry, and the signature is an
HMAC-SHA256 over the payload bytes with a secret only the matchmaker and the
game servers hold. MatchManager.ApprovalCheck in the Unity repo verifies it.

Read here rather than in mm.config so the ecs_events function, which never
issues tokens, doesn't need the secret in its environment.
"""
import base64
import hashlib
import hmac
import json
import os
import time

from mm import config

SECRET = os.environ["JOIN_TOKEN_SECRET"].encode()


def sign(player_id, session_id):
    payload = json.dumps({
        "sub": player_id,
        "sid": session_id,
        "exp": int(time.time()) + config.JOIN_TOKEN_TTL_SECONDS,
    }, separators = (",", ":")).encode()

    signature = hmac.new(SECRET, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(signature)}"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
