"""Compustat preflight check.

Determines whether Compustat Annual and Quarterly ECS tasks need to run on
this pipeline invocation by querying SEC EDGAR for recently-filed 10-K and
10-Q forms and intersecting against the configured universe.

Returns:
    {
        "annual_has_new":    bool,
        "quarterly_has_new": bool,
        "last_run_at":       ISO8601 string (UTC),
        "reason":            short string explaining the decision
    }

The handler is idempotent: it always writes back an updated manifest at
s3://<bucket>/<manifest_prefix>/manifest.json on success.

Fail-safe behavior:
    - No manifest present  -> has_new=true (cold start, run Compustat)
    - Manifest age > MAX_SKIP_DAYS  -> has_new=true (force periodic refresh)
    - EDGAR feed unreachable / parse error  -> has_new=true (don't silently skip)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

S3_BUCKET        = os.environ["S3_BUCKET"]
UNIVERSE_KEY     = os.environ.get("UNIVERSE_KEY", "data-ingress/Static/universe.csv")
MANIFEST_KEY     = os.environ.get("MANIFEST_KEY", "data-ingress/cache/compustat_preflight/manifest.json")
EDGAR_IDENTITY   = os.environ.get("EDGAR_IDENTITY", "EuclideanResearch contact@example.com")
MAX_SKIP_DAYS    = int(os.environ.get("MAX_SKIP_DAYS", "7"))
EDGAR_FEED_LIMIT = int(os.environ.get("EDGAR_FEED_LIMIT", "100"))

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

s3 = boto3.client("s3")


def _http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": EDGAR_IDENTITY, "Accept-Encoding": "gzip, deflate"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _load_universe_ciks() -> set[str]:
    """Read universe.csv from S3 and return CIKs as zero-padded 10-digit strings."""
    obj = s3.get_object(Bucket=S3_BUCKET, Key=UNIVERSE_KEY)
    text = obj["Body"].read().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    ciks: set[str] = set()
    for row in reader:
        raw = (row.get("cik") or row.get("CIK") or "").strip()
        if not raw:
            continue
        try:
            ciks.add(str(int(raw)).zfill(10))
        except ValueError:
            continue
    return ciks


def _load_manifest() -> dict | None:
    try:
        obj = s3.get_object(Bucket=S3_BUCKET, Key=MANIFEST_KEY)
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:
        log.warning("manifest read failed: %s", e)
        return None


def _save_manifest(manifest: dict) -> None:
    body = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    s3.put_object(Bucket=S3_BUCKET, Key=MANIFEST_KEY, Body=body, ContentType="application/json")


def _fetch_recent_filings(form_type: str) -> list[dict]:
    """Pull the EDGAR 'getcurrent' Atom feed for a given form type.

    Returns a list of {cik (10-digit), accession, updated} dicts, newest first.
    """
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        f"?action=getcurrent&type={form_type}&output=atom&count={EDGAR_FEED_LIMIT}"
    )
    raw = _http_get(url)
    root = ET.fromstring(raw)

    entries: list[dict] = []
    cik_re = re.compile(r"CIK=(\d{1,10})", re.IGNORECASE)
    accession_re = re.compile(r"Accession Number:\s*([0-9-]+)", re.IGNORECASE)

    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()

        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href", "") if link_el is not None else ""

        cik_match = cik_re.search(link) or cik_re.search(title) or cik_re.search(summary)
        if not cik_match:
            continue
        cik = cik_match.group(1).zfill(10)

        acc_match = accession_re.search(summary) or accession_re.search(title)
        accession = acc_match.group(1) if acc_match else ""

        entries.append({"cik": cik, "accession": accession, "updated": updated})

    return entries


def _has_new_filings(form_type: str, universe: set[str], last_seen: dict) -> tuple[bool, str, dict]:
    """Return (has_new, reason, updated_last_seen).

    last_seen maps cik -> latest accession we already processed.
    has_new is True if any new filing is in-universe and not in last_seen.
    """
    try:
        feed = _fetch_recent_filings(form_type)
    except Exception as e:
        log.error("EDGAR feed fetch failed for %s: %s", form_type, e)
        return True, f"edgar_feed_error:{form_type}", last_seen

    new_for_universe: list[dict] = []
    updated = dict(last_seen)
    for f in feed:
        if f["cik"] not in universe:
            continue
        prev = last_seen.get(f["cik"])
        if prev == f["accession"]:
            continue
        new_for_universe.append(f)
        if f["accession"]:
            updated[f["cik"]] = f["accession"]

    if not new_for_universe:
        return False, f"no_new_{form_type}_in_universe ({len(feed)} feed entries scanned)", updated

    sample = ", ".join(f"{f['cik']}:{f['accession']}" for f in new_for_universe[:5])
    return True, f"new_{form_type}_count={len(new_for_universe)} (e.g. {sample})", updated


def lambda_handler(event, context):
    now = datetime.now(timezone.utc)
    log.info("preflight starting: bucket=%s manifest=%s", S3_BUCKET, MANIFEST_KEY)

    universe = _load_universe_ciks()
    log.info("loaded %d universe CIKs", len(universe))

    manifest = _load_manifest()
    if manifest is None:
        log.info("no manifest -> cold start, force run both")
        new_manifest = {
            "annual":       {},
            "quarterly":    {},
            "last_run_at":  now.isoformat(),
        }
        _save_manifest(new_manifest)
        return {
            "annual_has_new":    True,
            "quarterly_has_new": True,
            "last_run_at":       now.isoformat(),
            "reason":            "cold_start_no_manifest",
        }

    last_run = manifest.get("last_run_at", "1970-01-01T00:00:00+00:00")
    try:
        last_run_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
    except Exception:
        last_run_dt = now - timedelta(days=MAX_SKIP_DAYS + 1)

    if now - last_run_dt > timedelta(days=MAX_SKIP_DAYS):
        log.info("manifest age > %d days -> force run", MAX_SKIP_DAYS)
        annual_has_new, annual_reason       = True, f"manifest_stale_{(now-last_run_dt).days}d"
        quarterly_has_new, quarterly_reason = True, f"manifest_stale_{(now-last_run_dt).days}d"
        annual_seen    = manifest.get("annual", {})
        quarterly_seen = manifest.get("quarterly", {})
    else:
        annual_seen    = manifest.get("annual", {})
        quarterly_seen = manifest.get("quarterly", {})

        annual_has_new, annual_reason, annual_seen = _has_new_filings("10-K", universe, annual_seen)
        # 10-Q only matters for quarterly; some companies (20-F filers) only file annuals
        quarterly_has_new, quarterly_reason, quarterly_seen = _has_new_filings("10-Q", universe, quarterly_seen)

    new_manifest = {
        "annual":      annual_seen,
        "quarterly":   quarterly_seen,
        "last_run_at": now.isoformat(),
    }
    _save_manifest(new_manifest)

    result = {
        "annual_has_new":    bool(annual_has_new),
        "quarterly_has_new": bool(quarterly_has_new),
        "last_run_at":       now.isoformat(),
        "reason":            f"annual: {annual_reason} | quarterly: {quarterly_reason}",
    }
    log.info("preflight result: %s", json.dumps(result))
    return result
