"""Universe downloader — step 1 of 3.

Downloads company_tickers_exchange.json from SEC EDGAR, filters to major US
exchanges, loads the existing SIC cache from S3, splits CIKs into two chunks,
writes a work manifest to S3, then asynchronously invokes universe_sic_worker
with chunk_index=0 to begin SIC enrichment.
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

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET              = os.environ["S3_BUCKET"]
UNIVERSE_KEY           = os.environ.get("UNIVERSE_KEY", "data-ingress/Static/universe.csv")
MANIFEST_PREFIX        = os.environ.get("MANIFEST_PREFIX", "universe/work")
SIC_WORKER_FUNCTION    = os.environ["SIC_WORKER_FUNCTION_NAME"]
EDGAR_IDENTITY         = os.environ.get("EDGAR_IDENTITY", "EuclideanResearch contact@example.com")

US_EXCHANGES = frozenset({"Nasdaq", "NYSE", "NYSE Arca", "NYSE American", "Cboe BZX", "BATS"})

MAX_RETRIES     = 3
RETRY_BACKOFF_S = 2.0

s3      = boto3.client("s3")
lambda_ = boto3.client("lambda")


def _get_json(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": EDGAR_IDENTITY, "Accept-Encoding": "gzip"},
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode())
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(RETRY_BACKOFF_S * attempt)


def _fetch_all_tickers() -> list[dict]:
    log.info("Downloading company_tickers_exchange.json from SEC EDGAR")
    data = _get_json("https://www.sec.gov/files/company_tickers_exchange.json")
    idx = {name: i for i, name in enumerate(data["fields"])}
    tickers = [
        {
            "cik":      str(row[idx["cik"]]).zfill(10),
            "name":     row[idx["name"]],
            "ticker":   row[idx["ticker"]],
            "exchange": row[idx["exchange"]],
        }
        for row in data["data"]
    ]
    log.info("Total SEC-registered tickers: %d", len(tickers))
    return tickers


def _filter_by_exchange(tickers: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for t in tickers:
        if t["exchange"] in US_EXCHANGES and t["ticker"] not in seen:
            seen.add(t["ticker"])
            out.append(t)
    log.info("Tickers on major US exchanges: %d", len(out))
    return out


def _load_existing_sic_cache() -> dict[str, str]:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=UNIVERSE_KEY)
        text = obj["Body"].read().decode("utf-8", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        cache: dict[str, str] = {}
        for row in reader:
            cik = (row.get("cik") or "").strip().zfill(10)
            sic = (row.get("sic") or "").strip()
            if cik and sic:
                cache[cik] = sic
        log.info("Loaded existing SIC cache: %d entries", len(cache))
        return cache
    except s3.exceptions.NoSuchKey:
        log.info("No existing universe.csv found — cold start")
        return {}
    except Exception as e:
        log.warning("Failed to load existing universe.csv: %s", e)
        return {}


def lambda_handler(event, context):
    all_tickers   = _fetch_all_tickers()
    filtered      = _filter_by_exchange(all_tickers)
    existing_sic  = _load_existing_sic_cache()

    ciks = [t["cik"] for t in filtered]
    mid  = len(ciks) // 2
    chunk_0 = ciks[:mid]
    chunk_1 = ciks[mid:]
    log.info("Split: chunk_0=%d CIKs, chunk_1=%d CIKs", len(chunk_0), len(chunk_1))

    manifest = {
        "all_tickers":  filtered,   # [{cik, ticker, name, exchange}]
        "existing_sic": existing_sic,
        "chunk_0":      chunk_0,
        "chunk_1":      chunk_1,
    }
    manifest_key = f"{MANIFEST_PREFIX}/manifest.json"
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=manifest_key,
        Body=json.dumps(manifest, separators=(",", ":")),
        ContentType="application/json",
    )
    log.info("Wrote manifest to s3://%s/%s", S3_BUCKET, manifest_key)

    lambda_.invoke(
        FunctionName=SIC_WORKER_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({"chunk_index": 0}),
    )
    log.info("Invoked %s with chunk_index=0", SIC_WORKER_FUNCTION)

    return {
        "status":        "ok",
        "total_tickers": len(filtered),
        "chunk_0_size":  len(chunk_0),
        "chunk_1_size":  len(chunk_1),
        "cache_size":    len(existing_sic),
    }
