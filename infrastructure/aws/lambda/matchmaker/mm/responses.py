import json
from decimal import Decimal


class MatchmakerError(Exception):
    """Something went wrong that the caller should hear a real reason for."""


class NotFoundError(Exception):
    """The request named something that doesn't exist."""


def respond(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default = decimal_default),
    }


def decimal_default(value):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Not serializable: {type(value)}")
