"""EDGAR filing poller — daily scheduled Lambda.

For each CIK in the universe, checks the EDGAR submissions API for recent
10-K and 10-Q filings within LOOKBACK_DAYS. For each filing not already
present in S3 as a parquet file, publishes an SQS message for the
edgar_ai_worker to process.

As a free side effect of already hitting the submissions API, also detects
SIC code changes and writes the updated universe.csv back to S3.
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
UNIVERSE_KEY   = os.environ.get("UNIVERSE_KEY", "universe/universe.csv")
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
        no_xbrl = (row.get("no_xbrl") or "").strip() == "1"
        if cik:
            rows.append({
                "cik":     str(int(cik)).zfill(10),
                "ticker":  ticker,
                "sic":     sic,
                "no_xbrl": no_xbrl,
            })
    no_xbrl_count = sum(1 for r in rows if r["no_xbrl"])
    log.info("Loaded universe: %d companies (%d flagged no_xbrl)", len(rows), no_xbrl_count)
    return rows


def _write_universe(rows: list[dict], no_xbrl_updates: dict[str, bool] | None = None) -> None:
    if no_xbrl_updates:
        for row in rows:
            if row["cik"] in no_xbrl_updates:
                row["no_xbrl"] = no_xbrl_updates[row["cik"]]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ticker", "cik", "sic", "no_xbrl"])
    writer.writeheader()
    for row in rows:
        writer.writerow({
            "ticker":  row["ticker"],
            "cik":     row["cik"],
            "sic":     row["sic"],
            "no_xbrl": "1" if row.get("no_xbrl") else "",
        })
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=UNIVERSE_KEY,
        Body=buf.getvalue().encode("utf-8"),
        ContentType="text/csv",
    )
    log.info("Wrote updated universe.csv to s3://%s/%s", S3_BUCKET, UNIVERSE_KEY)


def _s3_key(form_type: str, cik: str, report_date: str) -> str:
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    return f"data-ingress/filings/{folder}/{cik}/{report_date}.parquet"


def _file_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("404", "NoSuchKey", "403"):
            return False
        raise


def _is_no_xbrl_filing(key: str) -> bool:
    """Return True when the existing parquet was written as a no_facts stub.

    Uses the S3 object metadata tag set by the edgar_ai_worker — avoids reading
    the parquet body or depending on pyarrow in the poller.
    """
    try:
        resp = s3.head_object(Bucket=S3_BUCKET, Key=key)
        return resp.get("Metadata", {}).get("xbrl_status") == "no_facts"
    except ClientError:
        return False


def _filings_for_company(row: dict, lookback_cutoff: str) -> tuple[list[dict], str]:
    """Return (filings, current_sic) for this company.

    filings: list of {cik, ticker, sic, form_type, accession_number, report_date}
    current_sic: SIC code as reported by EDGAR submissions API (may differ from cached)
    """
    cik = row["cik"]
    try:
        data = _get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
    except Exception as e:
        log.warning("Failed to fetch submissions for CIK %s: %s", cik, e)
        return [], row["sic"]

    current_sic = str(data.get("sic") or row["sic"]).strip()

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
            "sic":              current_sic,
            "form_type":        form,
            "accession_number": acc,
            "report_date":      report_date,
        })
    return results, current_sic


def lambda_handler(event, context):
    universe = _load_universe()
    lookback_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    log.info("Polling filings filed on or after %s", lookback_cutoff)

    filings_checked = 0
    messages_sent   = 0
    skipped_no_xbrl = 0
    sic_updates: dict[str, str] = {}         # cik -> new_sic
    no_xbrl_updates: dict[str, bool] = {}    # cik -> True (newly confirmed no-XBRL)

    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]
        # Don't hit EDGAR API for companies already confirmed to have no XBRL
        active_batch = [row for row in batch if not row.get("no_xbrl")]
        skipped_no_xbrl += len(batch) - len(active_batch)
        if not active_batch:
            continue
        with ThreadPoolExecutor(max_workers=len(active_batch)) as pool:
            futs = {pool.submit(_filings_for_company, row, lookback_cutoff): row for row in active_batch}
            for fut in as_completed(futs):
                row = futs[fut]
                filings, current_sic = fut.result()

                if current_sic and current_sic != row["sic"]:
                    log.info(
                        "SIC change detected: %s %s -> %s",
                        row["ticker"], row["sic"], current_sic,
                    )
                    sic_updates[row["cik"]] = current_sic

                for filing in filings:
                    filings_checked += 1
                    key = _s3_key(filing["form_type"], filing["cik"], filing["report_date"])
                    if _file_exists(key):
                        # Check if the existing parquet is a no_facts stub.
                        # If so, flag the company so we never queue it again.
                        if _is_no_xbrl_filing(key) and row["cik"] not in no_xbrl_updates:
                            log.info(
                                "Flagging %s (%s) as no_xbrl — existing parquet is a stub",
                                row["ticker"], row["cik"],
                            )
                            no_xbrl_updates[row["cik"]] = True
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

    needs_write = bool(sic_updates or no_xbrl_updates)
    if needs_write:
        if sic_updates:
            log.info("Updating %d SIC code(s) in universe.csv", len(sic_updates))
            for row in universe:
                if row["cik"] in sic_updates:
                    row["sic"] = sic_updates[row["cik"]]
        if no_xbrl_updates:
            log.info("Flagging %d no-XBRL companies in universe.csv", len(no_xbrl_updates))
        _write_universe(universe, no_xbrl_updates=no_xbrl_updates)

    log.info(
        "Done: %d filings checked, %d messages sent, %d SIC updates, %d no_xbrl flags, %d skipped",
        filings_checked, messages_sent, len(sic_updates), len(no_xbrl_updates), skipped_no_xbrl,
    )
    return {
        "universe_size":     len(universe),
        "lookback_cutoff":   lookback_cutoff,
        "filings_checked":   filings_checked,
        "messages_sent":     messages_sent,
        "sic_updates":       len(sic_updates),
        "no_xbrl_flags":     len(no_xbrl_updates),
        "skipped_no_xbrl":   skipped_no_xbrl,
    }
