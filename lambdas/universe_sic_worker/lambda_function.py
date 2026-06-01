"""Universe SIC worker — steps 2 and 3 of 3.

Invoked twice sequentially (by universe_downloader then by itself):

  chunk_index=0: enriches SIC for first half of CIKs, writes result_0.json,
                 then self-invokes with chunk_index=1.

  chunk_index=1: enriches SIC for second half, merges both results, applies
                 common-stock filtering, writes final universe.csv to S3,
                 and cleans up work files.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET       = os.environ["S3_BUCKET"]
UNIVERSE_KEY    = os.environ.get("UNIVERSE_KEY", "data-ingress/Static/universe.csv")
MANIFEST_PREFIX = os.environ.get("MANIFEST_PREFIX", "universe/work")
EDGAR_IDENTITY  = os.environ.get("EDGAR_IDENTITY", "EuclideanResearch contact@example.com")
SELF_FUNCTION   = os.environ["AWS_LAMBDA_FUNCTION_NAME"]

EXCLUDE_SIC = frozenset({"6726", "6770"})
_NON_COMMON_HYPHEN_SUFFIXES = frozenset(
    {"W", "WS", "WT", "R", "RT", "RI", "U", "UN", "UT", "P", "PR", "PFD"}
)
_NON_COMMON_HYPHEN_SUFFIX_RE = re.compile(r"^P[A-Z0-9]{1,3}$")

BATCH_SIZE      = 8
BATCH_PAUSE_S   = 1.0
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


def _fetch_sic(cik: str) -> str:
    try:
        data = _get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
        return str(data.get("sic", "") or "")
    except Exception:
        return ""


def _enrich_sic_chunk(ciks: list[str], existing_sic: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    to_fetch = [cik for cik in ciks if cik not in existing_sic]
    cached   = {cik: existing_sic[cik] for cik in ciks if cik in existing_sic}

    log.info("SIC enrichment: %d cached, %d to fetch", len(cached), len(to_fetch))

    fetched: dict[str, str] = {}
    for i in range(0, len(to_fetch), BATCH_SIZE):
        batch = to_fetch[i : i + BATCH_SIZE]
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futs = {pool.submit(_fetch_sic, cik): cik for cik in batch}
            for fut in as_completed(futs):
                fetched[futs[fut]] = fut.result()
        if i % 500 < BATCH_SIZE or i + BATCH_SIZE >= len(to_fetch):
            log.info("  fetched %d / %d", min(i + BATCH_SIZE, len(to_fetch)), len(to_fetch))
        time.sleep(BATCH_PAUSE_S)

    result.update(cached)
    result.update(fetched)
    return result


def _is_non_common(symbol: str, ticker_set: set[str]) -> bool:
    ticker = (symbol or "").upper().strip()
    if not ticker:
        return True
    if "-" in ticker:
        suffix = ticker.rsplit("-", 1)[-1]
        if suffix in _NON_COMMON_HYPHEN_SUFFIXES:
            return True
        if _NON_COMMON_HYPHEN_SUFFIX_RE.match(suffix):
            return True
    if len(ticker) == 5 and ticker[-1] in {"W", "R", "U", "Z"} and ticker[:-1] in ticker_set:
        return True
    if len(ticker) == 6 and ticker[-2:] in {"WS", "WT", "RW", "RT", "RU"} and ticker[:-2] in ticker_set:
        return True
    return False


def _filter_common_stocks(tickers: list[dict]) -> list[dict]:
    ticker_set = {t["ticker"] for t in tickers}
    out = []
    excl_sic = excl_sym = 0
    for t in tickers:
        sic = t.get("sic", "")
        if not sic or sic in EXCLUDE_SIC:
            excl_sic += 1
            continue
        if _is_non_common(t.get("ticker", ""), ticker_set):
            excl_sym += 1
            continue
        out.append(t)
    log.info("filter_common_stocks: excluded %d SIC, %d symbol → %d remaining",
             excl_sic, excl_sym, len(out))
    return out


def _load_manifest() -> dict:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{MANIFEST_PREFIX}/manifest.json")
    return json.loads(obj["Body"].read())


def _load_result_0() -> dict[str, str]:
    obj = s3.get_object(Bucket=S3_BUCKET, Key=f"{MANIFEST_PREFIX}/result_0.json")
    return json.loads(obj["Body"].read())


def _write_universe_csv(tickers: list[dict]) -> bytes:
    tickers_sorted = sorted(tickers, key=lambda t: t["ticker"])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["ticker", "cik", "sic"])
    writer.writeheader()
    for t in tickers_sorted:
        writer.writerow({"ticker": t["ticker"], "cik": t["cik"], "sic": t["sic"]})
    return buf.getvalue().encode("utf-8")


def _handle_chunk_0(manifest: dict) -> dict:
    sic_result = _enrich_sic_chunk(manifest["chunk_0"], manifest["existing_sic"])

    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{MANIFEST_PREFIX}/result_0.json",
        Body=json.dumps(sic_result, separators=(",", ":")),
        ContentType="application/json",
    )
    log.info("Wrote result_0.json (%d entries)", len(sic_result))

    lambda_.invoke(
        FunctionName=SELF_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({"chunk_index": 1}),
    )
    log.info("Self-invoked with chunk_index=1")

    return {"chunk_index": 0, "count": len(sic_result)}


def _handle_chunk_1(manifest: dict) -> dict:
    result_0   = _load_result_0()
    sic_result = _enrich_sic_chunk(manifest["chunk_1"], manifest["existing_sic"])

    merged_sic = {**result_0, **sic_result}
    log.info("Merged SIC: %d total entries", len(merged_sic))

    enriched = []
    for t in manifest["all_tickers"]:
        cik = t["cik"]
        sic = merged_sic.get(cik, "")
        enriched.append({"ticker": t["ticker"], "cik": cik, "sic": sic})

    final = _filter_common_stocks(enriched)

    csv_bytes = _write_universe_csv(final)
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=UNIVERSE_KEY,
        Body=csv_bytes,
        ContentType="text/csv",
    )
    log.info("Wrote universe.csv: %d rows → s3://%s/%s", len(final), S3_BUCKET, UNIVERSE_KEY)

    for key in [f"{MANIFEST_PREFIX}/manifest.json", f"{MANIFEST_PREFIX}/result_0.json"]:
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception as e:
            log.warning("Failed to delete %s: %s", key, e)

    return {"chunk_index": 1, "row_count": len(final)}


def lambda_handler(event, context):
    chunk_index = int((event or {}).get("chunk_index", 0))
    log.info("universe_sic_worker starting: chunk_index=%d", chunk_index)

    manifest = _load_manifest()

    if chunk_index == 0:
        return _handle_chunk_0(manifest)
    else:
        return _handle_chunk_1(manifest)
