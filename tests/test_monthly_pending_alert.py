import importlib.util
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


SOURCE = Path(__file__).parents[1] / "lambdas/monthly_pending_alert/lambda_function.py"
spec = importlib.util.spec_from_file_location("monthly_pending_alert", SOURCE)
alert = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alert)


def test_nine_am_checkpoint_sends_one_specific_pending_alert(monkeypatch):
    now = datetime(2026, 12, 1, 14, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alert, "datetime", SimpleNamespace(now=lambda tz: now))
    for name, value in {"S3_BUCKET": "bucket", "STATUS_FUNCTION": "status",
                        "NOTIFICATIONS_TOPIC_ARN": "topic"}.items():
        monkeypatch.setenv(name, value)
    calls = []

    class S3:
        def put_object(self, **kwargs):
            calls.append(("marker", kwargs["Key"]))

    class Lambda:
        def invoke(self, **kwargs):
            return {"Payload": io.BytesIO(json.dumps({
                "as_of_month": 202611, "stage": "sources_pending",
                "pending_sources": [{"dataset": "ibes_eps_adjusted"}],
            }).encode())}

    class SNS:
        def publish(self, **kwargs):
            calls.append(("alert", json.loads(kwargs["Message"])["status"]["stage"]))

    monkeypatch.setattr(alert.boto3, "client", lambda name: {
        "s3": S3(), "lambda": Lambda(), "sns": SNS(),
    }[name])
    result = alert.lambda_handler({}, None)
    assert result["status"] == "pending_alert_sent"
    assert calls == [("marker", "control-plane/monthly-status-alerts/202611.json"),
                     ("alert", "sources_pending")]


def test_checkpoint_ignores_other_new_york_hours(monkeypatch):
    now = datetime(2026, 12, 1, 13, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(alert, "datetime", SimpleNamespace(now=lambda tz: now))
    assert alert.lambda_handler({}, None) == {"status": "outside_checkpoint"}
