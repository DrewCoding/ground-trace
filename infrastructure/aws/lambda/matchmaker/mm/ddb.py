import boto3
from botocore.exceptions import ClientError

from mm import config

_dynamodb = boto3.resource("dynamodb")

sessions = _dynamodb.Table(config.SESSIONS_TABLE)
queue = _dynamodb.Table(config.QUEUE_TABLE)

def condition_failed(exc):
    return (
        isinstance(exc, ClientError)
        and exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
    )