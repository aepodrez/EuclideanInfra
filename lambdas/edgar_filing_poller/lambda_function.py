"""EDGAR filing poller — daily scheduled Lambda.

For each CIK in the universe, checks the EDGAR submissions API for recent
10-K and 10-Q filings within LOOKBACK_DAYS. For each filing not already
present in S3 as a parquet file, publishes an SQS message for the
edgar_ai_worker to process.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET      = os.environ["S3_BUCKET"]
UNIVERSE_KEY   = os.environ.get("UNIVERSE_KEY", "data-ingress/Static/universe.csv")
SQS_QUEUE_URL  = os.environ["SQS_QUEUE_URL"]
EDGAR_IDENTITY = os.environ.get("EDGAR_IDENTITY", "EuclideanResearch contact@example.com")
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "90"))

BATCH_SIZE      = 8
BATCH_PAUSE_S   = 1.0
MAX_RETRIES     = 3
RETRY_BACKOFF_S = 2.0

s3  = boto3.client("s3")
sqs = boto3.client("sqs")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": EDGAR_IDENTITY, "Accept-Encoding": "gzip"},
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_BACKOFF_S * attempt)


def _load_universe() -> list[dict]:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=UNIVERSE_KEY)
    text = obj["Body"].read().decode("utf-8", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        cik = (row.get("cik") or "").strip()
        ticker = (row.get("ticker") or "").strip().upper()
        sic = (row.get("sic") or "").strip()
        if cik:
            rows.append({"cik": str(int(cik)).zfill(10), "ticker": ticker, "sic": sic})
    log.info("Loaded universe: %d companies", len(rows))
    return rows


def _s3_key(form_type: str, cik: str, report_date: str) -> str:
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    return f"data-ingress/filings/{folder}/{cik}/{report_date}.parquet"


def _file_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise


def _filings_for_company(row: dict, lookback_cutoff: str) -> list[dict]:
    """Return list of {cik, ticker, sic, form_type, accession_number, report_date}
    for 10-K / 10-Q filings filed on or after lookback_cutoff (YYYY-MM-DD)."""
    cik = row["cik"]
    try:
        data = _get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as e:
        log.warning("Failed to fetch submissions for CIK %s: %s", cik, e)
        return []

    recent = data.get("filings", {}).get("recent", {})
    accessions   = recent.get("accessionNumber", [])
    forms        = recent.get("form", [])
    report_dates = recent.get("reportDate", [])
    filed_dates  = recent.get("filingDate", [])

    results = []
    for acc, form, report_date, filed_date in zip(accessions, forms, report_dates, filed_dates):
        if form not in ("10-K", "10-Q"):
            continue
        if filed_date < lookback_cutoff:
            continue
        if not report_date or not acc:
            continue
        results.append({
            "cik":              cik,
            "ticker":           row["ticker"],
            "sic":              row["sic"],
            "form_type":        form,
            "accession_number": acc,
            "report_date":      report_date,
        })
    return results


def lambda_handler(event, context):
    universe = _load_universe()
    lookback_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    log.info("Polling filings filed on or after %s", lookback_cutoff)

    filings_checked = 0
    messages_sent   = 0

    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futs = {pool.submit(_filings_for_company, row, lookback_cutoff): row for row in batch}
            for fut in as_completed(futs):
                filings = fut.result()
                for filing in filings:
                    filings_checked += 1
                    key = _s3_key(filing["form_type"], filing["cik"], filing["report_date"])
                    if _file_exists(key):
                        continue
                    try:
                        sqs.send_message(
                            QueueUrl=SQS_QUEUE_URL,
                            MessageBody=json.dumps(filing),
                        )
                        messages_sent += 1
                        log.info("Queued %s %s %s", filing["ticker"], filing["form_type"], filing["report_date"])
                    except Exception as e:
                        log.error("Failed to queue %s %s: %s", filing["cik"], filing["accession_number"], e)
        time.sleep(BATCH_PAUSE_S)

    log.info("Done: %d filings checked, %d messages sent", filings_checked, messages_sent)
    return {
        "universe_size":    len(universe),
        "lookback_cutoff":  lookback_cutoff,
        "filings_checked":  filings_checked,
        "messages_sent":    messages_sent,
    }
