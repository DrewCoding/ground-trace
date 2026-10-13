import boto3
from botocore.exceptions import ClientError

from mm import config

_dynamodb = boto3.resource("dynamodb")

sessions = _dynamodb.Table(config.SESSIONS_TABLE)
queue = _dynamodb.Table(config.QUEUE_TABLE)

# UpdateItem creates the item when the key doesn't exist. Every update in this
# package that targets a row someone else may have deleted - a ticket the
# player cancelled, a session whose task stopped - carries this condition, or
# it would write back a partial row. One with no expiresAt is never garbage
# collected, and a session one counts against the server cap forever.
EXISTS_TICKET = "attribute_exists(ticketId)"
EXISTS_SESSION = "attribute_exists(sessionId)"


def condition_failed(exc):
    return (
        isinstance(exc, ClientError)
        and exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
    )
