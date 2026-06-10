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
        if cik:
            rows.append({"cik": str(int(cik)).zfill(10), "ticker": ticker, "sic": sic})
    log.info("Loaded universe: %d companies", len(rows))
    return rows


def _write_universe(rows: list[dict]) -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ticker", "cik", "sic"])
    writer.writeheader()
    writer.writerows(rows)
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


def _get_existing_accession(form_type: str, cik: str) -> str | None:
    """Return the accession number stored in S3 metadata for the most recent
    parquet for this CIK + form family, or None if no parquet exists.

    Uses a prefix listing to avoid date-mismatch false negatives, then HEADs
    the most recent object to read its accession metadata.
    """
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    prefix = f"data-ingress/filings/{folder}/{cik}/"
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=10)
    keys = [obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".parquet")]
    if not keys:
        return None
    # Most recent parquet by filename (report_date)
    latest_key = sorted(keys, reverse=True)[0]
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=latest_key)
        return head.get("Metadata", {}).get("accession")
    except ClientError:
        return None



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
    cik_int = str(int(cik))  # CIK without leading zeros, for accession prefix check
    for acc, form, report_date, filed_date in zip(accessions, forms, report_dates, filed_dates):
        if form not in ("10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"):
            continue
        if filed_date < lookback_cutoff:
            continue
        if not report_date or not acc:
            continue
        # Skip filings submitted by a different entity (combined/parent filings).
        # Accession format: {filer_CIK}-{year}-{seq}. If the filer CIK doesn't
        # match this company's CIK, the XBRL is filed under the parent and we
        # won't find it under this subsidiary's companyfacts.
        filer_cik = acc.split("-")[0].lstrip("0") or "0"
        if filer_cik != cik_int:
            log.debug("Skipping %s for CIK %s — filed by parent CIK %s", acc, cik, filer_cik)
            continue
        results.append({
            "cik":              cik,
            "ticker":           row["ticker"],
            "sic":              current_sic,
            "form_type":        form,
            "accession_number": acc,
            "report_date":      report_date,
        })
    results.sort(key=lambda f: f["report_date"], reverse=True)
    return results, current_sic


def _latest_parquet_date(form_type: str, cik: str) -> str | None:
    """Return the most recent report_date (YYYY-MM-DD) from parquet filenames, or None."""
    folder = "annual" if "10-K" in form_type or "20-F" in form_type else "quarterly"
    prefix = f"data-ingress/filings/{folder}/{cik}/"
    resp = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=10)
    keys = [obj["Key"] for obj in resp.get("Contents", []) if obj["Key"].endswith(".parquet")]
    if not keys:
        return None
    dates = sorted([k.split("/")[-1].replace(".parquet", "") for k in keys], reverse=True)
    return dates[0]


def _needs_edgar_check(cik: str, today: str) -> bool:
    """Return True if we should hit EDGAR for this company.

    Skip only if BOTH filings exist AND are recent enough that a new one
    can't be due yet (quarterly < 80 days old, annual < 350 days old).
    """
    from datetime import date as date_type

    def _days_old(report_date: str) -> int:
        try:
            return (date_type.fromisoformat(today) - date_type.fromisoformat(report_date)).days
        except Exception:
            return 9999

    annual_date    = _latest_parquet_date("10-K", cik)
    quarterly_date = _latest_parquet_date("10-Q", cik)

    annual_ok    = annual_date    is not None and _days_old(annual_date)    < 350
    quarterly_ok = quarterly_date is not None and _days_old(quarterly_date) < 80

    return not (annual_ok and quarterly_ok)


def lambda_handler(event, context):
    universe = _load_universe()
    lookback_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log.info("Polling filings filed on or after %s", lookback_cutoff)

    filings_checked = 0
    messages_sent   = 0
    sic_updates: dict[str, str] = {}  # cik -> new_sic for any changed SIC codes

    for i in range(0, len(universe), BATCH_SIZE):
        batch = universe[i : i + BATCH_SIZE]
        # Skip EDGAR API call for companies whose filings are fresh enough that
        # no new filing can be due yet — saves ~70% of EDGAR calls on typical runs.
        batch = [row for row in batch if _needs_edgar_check(row["cik"], today)]
        if not batch:
            continue
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futs = {pool.submit(_filings_for_company, row, lookback_cutoff): row for row in batch}
            for fut in as_completed(futs):
                row = futs[fut]
                filings, current_sic = fut.result()

                if current_sic and current_sic != row["sic"]:
                    log.info(
                        "SIC change detected: %s %s -> %s",
                        row["ticker"], row["sic"], current_sic,
                    )
                    sic_updates[row["cik"]] = current_sic

                # Queue at most one 10-K and one 10-Q per company (most recent of each).
                # EDGAR returns filings newest-first, so the first match per form type
                # is the latest. Skip if already in S3.
                seen_forms: set[str] = set()
                for filing in filings:
                    form = filing["form_type"]
                    base = "annual" if "10-K" in form or "20-F" in form else "quarterly"
                    if base in seen_forms:
                        continue
                    seen_forms.add(base)
                    filings_checked += 1
                    existing_acc = _get_existing_accession(form, filing["cik"])
                    if existing_acc != filing["accession_number"]:
                        try:
                            sqs.send_message(
                                QueueUrl=SQS_QUEUE_URL,
                                MessageBody=json.dumps(filing),
                            )
                            messages_sent += 1
                            log.info("Queued %s %s %s", filing["ticker"], form, filing["report_date"])
                        except Exception as e:
                            log.error("Failed to queue %s %s: %s", filing["cik"], filing["accession_number"], e)
                    if len(seen_forms) == 2:
                        break
        time.sleep(BATCH_PAUSE_S)

    if sic_updates:
        log.info("Updating %d SIC code(s) in universe.csv", len(sic_updates))
        for row in universe:
            if row["cik"] in sic_updates:
                row["sic"] = sic_updates[row["cik"]]
        _write_universe(universe)

    log.info("Done: %d filings checked, %d messages sent, %d SIC updates", filings_checked, messages_sent, len(sic_updates))
    return {
        "universe_size":    len(universe),
        "lookback_cutoff":  lookback_cutoff,
        "filings_checked":  filings_checked,
        "messages_sent":    messages_sent,
        "sic_updates":      len(sic_updates),
    }
