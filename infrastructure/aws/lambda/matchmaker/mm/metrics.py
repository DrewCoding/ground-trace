import json
import time

from mm import config
from mm.responses import decimal_default


def emit_metric(name, value, unit = "Count", timestamp = None, **properties):
    """Publish a metric via CloudWatch Embedded Metric Format.

    EMF works by writing a specially shaped JSON line to stdout, which
    CloudWatch parses out of the log stream and turns into a metric. That
    avoids a PutMetricData call on every invocation - no extra latency in the
    request path, no extra IAM permission, and no failure mode if the metrics
    API is having a bad day.

    Uses print rather than the logger on purpose: Lambda's default log format
    prefixes each line with a timestamp, request id and level, which would
    stop CloudWatch recognising the JSON as EMF.

    timestamp (epoch milliseconds) is for events reported after the fact, so
    the data point lands when the thing happened rather than when we heard.

    properties are searchable in the logs but are not dimensions: each unique
    dimension combination is a separately billed metric, so per-session
    values like sessionId must never become one.
    """
    payload = {
        "_aws": {
            "Timestamp": timestamp if timestamp is not None else int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": config.METRICS_NAMESPACE,
                "Dimensions": [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
    }
    payload.update(properties)
    print(json.dumps(payload, default = decimal_default))
