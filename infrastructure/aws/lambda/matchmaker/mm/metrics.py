import json
import time
from mm import config
from mm.responses import decimal_default

def emit_metric(name, value, unit = "Count", **properties):
    """Publish a metric via CloudWatch Embedded Metric Format.

    EMF works by writing a specially shaped JSON line to stdout, which
    CloudWatch parses out of the log stream and turns into a metric. That
    avoids a PutMetricData call on every invocation - no extra latency in the
    request path, no extra IAM permission, and no failure mode if the metrics
    API is having a bad day.

    Uses print rather than the logger on purpose: Lambda's default log format
    prefixes each line with a timestamp, request id and level, which would
    stop CloudWatch recognising the JSON as EMF.
    """
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
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
