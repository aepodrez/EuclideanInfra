"""One bounded status alert at 9 a.m. New York time on each month first."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError


def _previous_month(now: datetime) -> int:
    year, month = now.year, now.month - 1
    if month == 0:
        year, month = year - 1, 12
    return year * 100 + month


def lambda_handler(event, context):
    del event, context
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("America/New_York"))
    if local.day != 1 or local.hour != 9:
        return {"status": "outside_checkpoint"}
    month = _previous_month(local)
    bucket = os.environ["S3_BUCKET"]
    s3 = boto3.client("s3")
    lambda_client = boto3.client("lambda")
    status_response = lambda_client.invoke(
        FunctionName=os.environ["STATUS_FUNCTION"],
        InvocationType="RequestResponse",
        Payload=json.dumps({"mode": "monthly_pending_status", "as_of_month": month}).encode(),
    )
    if status_response.get("FunctionError"):
        status = {"as_of_month": month, "stage": "unreadable_status_evidence"}
    else:
        try:
            status = json.loads(status_response["Payload"].read())
        except (ValueError, TypeError):
            status = {"as_of_month": month, "stage": "unreadable_status_evidence"}
        if status.get("statusCode") in {422, 424} or status.get("as_of_month") != month:
            status = {"as_of_month": month, "stage": "unreadable_status_evidence"}
    run_id = status.get("run_id")
    if run_id:
        key = f"portfolio-construction/runs/{run_id}/optimal_portfolio_meta.json"
        try:
            raw = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {
                "NoSuchKey", "404", "NotFound",
            }:
                raise
        else:
            try:
                meta = json.loads(raw)
                if (meta.get("run_id") == run_id
                        and meta.get("portfolio_grade") == "production_point_in_time"
                        and meta.get("portfolio_sha256")):
                    portfolio = s3.get_object(
                        Bucket=bucket,
                        Key=f"portfolio-construction/runs/{run_id}/optimal_portfolio.csv",
                    )["Body"].read()
                    if hashlib.sha256(portfolio).hexdigest() == meta["portfolio_sha256"]:
                        return {"status": "portfolio_certified", "run_id": run_id}
            except (ClientError, ValueError, TypeError, KeyError):
                status["stage"] = "unreadable_portfolio_evidence"
    # Create a single marker before the notification. Retrying this Lambda
    # cannot send repeated alerts for the same formation month.
    body = json.dumps({"as_of_month": month, "checked_at_utc": now.isoformat(),
                       "status": status}, sort_keys=True).encode()
    marker = f"control-plane/monthly-status-alerts/{month}.json"
    try:
        s3.put_object(Bucket=bucket, Key=marker, Body=body,
                      ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412"}:
            return {"status": "already_alerted", "as_of_month": month}
        raise
    message = json.dumps({"checkpoint": "9 a.m. ET", "as_of_month": month,
                          "status": status,
                          "evidence_sha256": hashlib.sha256(body).hexdigest()},
                         sort_keys=True)
    boto3.client("sns").publish(
        TopicArn=os.environ["NOTIFICATIONS_TOPIC_ARN"],
        Subject=f"Month-end portfolio pending at 9 a.m.: {month}",
        Message=message,
    )
    return {"status": "pending_alert_sent", "as_of_month": month,
            "stage": status.get("stage")}
