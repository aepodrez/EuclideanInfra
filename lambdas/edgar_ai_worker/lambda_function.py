"""Edgar AI Worker Lambda.

Triggered by SQS. Each message describes one new SEC filing (10-K or 10-Q).
Fetches XBRL facts, maps them to Compustat fields via Nova Lite (fast) and
Kimi K2.6 via OpenRouter (strong fallback), validates accounting identities,
and writes a single-row parquet to S3.

SQS message format:
    {
        "cik":              "0000320193",
        "ticker":           "AAPL",
        "form_type":        "10-K",
        "accession_number": "0000320193-24-000123",
        "sic":              "3674"   (optional)
    }

S3 output:
    data-ingress/filings/annual/{cik}/{report_date}.parquet
    data-ingress/filings/quarterly/{cik}/{report_date}.parquet
"""
from __future__ import annotations

import io
import json
import logging
import os

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

import xbrl_ai_mapper as mapper

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET = os.environ["S3_BUCKET"]

_s3      = boto3.client("s3")
_bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))

_OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]


def _s3_key(form_type: str, cik: str, report_date: str) -> str:
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    return f"data-ingress/filings/{folder}/{cik}/{report_date}.parquet"


def _write_parquet(row: dict, bucket: str, key: str) -> None:
    # Build a schema that marks diagnostic string columns explicitly
    arrays = {}
    for k, v in row.items():
        if isinstance(v, str):
            arrays[k] = pa.array([v], type=pa.string())
        elif isinstance(v, float):
            arrays[k] = pa.array([v], type=pa.float64())
        else:
            arrays[k] = pa.array([v], type=pa.string())

    table = pa.table(arrays)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    buf.seek(0)

    _s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=buf.read(),
        ContentType="application/octet-stream",
    )
    log.info("wrote parquet to s3://%s/%s", bucket, key)


def _process_message(body: dict) -> None:
    cik        = body["cik"]
    ticker     = body["ticker"]
    form_type  = body["form_type"]
    accession  = body["accession_number"]
    sic        = body.get("sic")

    row = mapper.map_filing(
        cik=cik,
        accession=accession,
        form_type=form_type,
        ticker=ticker,
        sic=sic,
        bedrock_client=_bedrock,
        openrouter_api_key=_OPENROUTER_API_KEY,
    )

    report_date = row.get("datadate", "unknown")
    key = _s3_key(form_type, cik, report_date)
    _write_parquet(row, S3_BUCKET, key)


def lambda_handler(event, context):
    records = event.get("Records", [])
    log.info("received %d SQS record(s)", len(records))

    failures = []
    for record in records:
        try:
            body = json.loads(record["body"])
            _process_message(body)
        except Exception as exc:
            log.exception("failed to process message %s: %s", record.get("messageId"), exc)
            failures.append({"itemIdentifier": record["messageId"]})

    # Return partial-batch failure response so only failed messages go to DLQ
    if failures:
        return {"batchItemFailures": failures}
