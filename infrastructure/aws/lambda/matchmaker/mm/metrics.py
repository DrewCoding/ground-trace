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

    timestamp (epoch milliseconds) dates the data point when the thing being
    measured happened earlier than now. Left as now, a late-delivered event
    would land in the wrong period on the dashboard.

    properties are searchable in the logs but are not dimensions: each unique
    dimension combination is a separately billed metric, so per-session
    values like sessionId must never become one.

    Each data point is published twice: once with no dimensions, and once
    per Orchestrator. The undimensioned copy is the metric dashboard.tf and
    the alarms were built against - adding a dimension to it would change its
    identity and blank them. The Orchestrator copy is what lets Fargate and
    Agones be compared from the same metrics. It has two values, and custom
    metrics are only billed for hours in which data arrives, so idle cost is
    unchanged.
    """
    if timestamp is None:
        timestamp = int(time.time() * 1000)

    payload = {
        "_aws": {
            "Timestamp": timestamp,
            "CloudWatchMetrics": [{
                "Namespace": config.METRICS_NAMESPACE,
                "Dimensions": [[], ["Orchestrator"]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
    }
    payload.update(properties)
    # After properties, so nothing passed in can mislabel the data point.
    payload["Orchestrator"] = config.ORCHESTRATOR
    print(json.dumps(payload, default = decimal_default))
