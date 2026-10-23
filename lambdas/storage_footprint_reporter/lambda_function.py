"""Bounded weekly S3 storage-footprint report using object metadata only."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

BUCKET = os.environ.get("S3_BUCKET", "euclidean-pipeline-954976294836")
REPORT_PREFIX = os.environ.get("REPORT_PREFIX", "control-plane/storage-footprint/")
MAX_OBJECTS = int(os.environ.get("MAX_OBJECTS", "5000000"))
MAX_PAGES = int(os.environ.get("MAX_LIST_REQUESTS", "6000"))
MIN_ARCHIVE_SIZE = 131072
LIST_COST_PER_1000 = 0.005
RAW_PREFIXES = (
    "data-ingress/pit/",
    "alternative-data/company-web/direct/raw/",
    "alternative-data/company-web/archive/raw/",
    "alternative-data/gdelt/raw/",
    "alternative-data/ats/raw/",
    "alternative-data/youtube/raw/",
    "alternative-data/common-crawl/raw/",
)
GROUPS = (
    ("pyData/Intermediate/", "Intermediate"),
    ("pyData/Predictors/", "Predictors"),
    ("data-ingress/pit/", "PIT"),
    ("alternative-data/company-web/", "company-web"),
    ("alternative-data/gdelt/", "GDELT"),
    ("alternative-data/ats/", "ATS"),
    ("alternative-data/common-crawl/", "Common Crawl"),
    ("alternative-data/_runs/", "published alternative-data runs"),
)
_s3 = boto3.client("s3")


class ScanLimitExceeded(RuntimeError):
    pass


def _group(key: str) -> str:
    for prefix, name in GROUPS:
        if key.startswith(prefix):
            return name
    return "Other"


def _storage_class(value: str | None) -> str:
    value = (value or "STANDARD").upper()
    if value == "GLACIER_IR":
        return "GLACIER_IR"
    if value in {"GLACIER", "DEEP_ARCHIVE"}:
        return "GLACIER"
    return "STANDARD"


def _empty_group() -> dict[str, Any]:
    return {
        "objects": 0, "bytes": 0,
        "storage_class_bytes": {"STANDARD": 0, "GLACIER_IR": 0, "GLACIER": 0},
        "new_7d_objects": 0, "new_7d_bytes": 0, "new_7d_standard_bytes": 0,
        "transition_eligible_objects": 0, "transition_eligible_bytes": 0,
    }


def build_report(s3: Any, bucket: str, now: datetime, *, max_objects: int = MAX_OBJECTS,
                 max_pages: int = MAX_PAGES) -> dict[str, Any]:
    groups = {name: _empty_group() for _, name in GROUPS}
    groups["Other"] = _empty_group()
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    token = None
    pages = 0
    objects = 0
    truncated = False
    reason = None
    while True:
        if pages >= max_pages:
            truncated, reason = True, "max-list-requests"
            break
        args = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            args["ContinuationToken"] = token
        page = s3.list_objects_v2(**args)
        pages += 1
        for item in page.get("Contents", []):
            objects += 1
            if objects > max_objects:
                truncated, reason = True, "max-objects"
                break
            key = str(item.get("Key") or "")
            size = int(item.get("Size", 0))
            modified = item.get("LastModified")
            storage = _storage_class(item.get("StorageClass"))
            row = groups[_group(key)]
            row["objects"] += 1
            row["bytes"] += size
            row["storage_class_bytes"][storage] += size
            if modified and modified >= cutoff_7d:
                row["new_7d_objects"] += 1
                row["new_7d_bytes"] += size
                if storage == "STANDARD":
                    row["new_7d_standard_bytes"] += size
            if (storage == "STANDARD" and size > MIN_ARCHIVE_SIZE and modified
                    and modified <= cutoff_30d and any(key.startswith(prefix) for prefix in RAW_PREFIXES)):
                row["transition_eligible_objects"] += 1
                row["transition_eligible_bytes"] += size
        if truncated or not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            truncated, reason = True, "missing-continuation-token"
            break

    totals = _empty_group()
    for row in groups.values():
        for key in ("objects", "bytes", "new_7d_objects", "new_7d_bytes",
                    "new_7d_standard_bytes", "transition_eligible_objects", "transition_eligible_bytes"):
            totals[key] += row[key]
        for storage, value in row["storage_class_bytes"].items():
            totals["storage_class_bytes"][storage] += value
    return {
        "schema_version": 1,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "bucket": bucket,
        "scan": {
            "status": "truncated" if truncated else "complete",
            "truncated": truncated,
            "reason": reason,
            "pages": pages,
            "estimated_list_cost_usd": round(pages / 1000 * LIST_COST_PER_1000, 6),
            "max_objects": max_objects,
            "max_list_requests": max_pages,
        },
        "totals": totals,
        "groups": groups,
    }


def _emit_metrics(report: dict[str, Any]) -> None:
    for name, row in [("Total", report["totals"]), *report["groups"].items()]:
        payload = {
            "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
                "Namespace": "Euclidean/StorageFootprint", "Dimensions": [["PrefixGroup"]],
                "Metrics": [
                    {"Name": "ObjectCount", "Unit": "Count"}, {"Name": "Bytes", "Unit": "Bytes"},
                    {"Name": "StandardBytes", "Unit": "Bytes"},
                    {"Name": "GlacierInstantBytes", "Unit": "Bytes"},
                    {"Name": "GlacierFlexibleBytes", "Unit": "Bytes"},
                    {"Name": "New7DayBytes", "Unit": "Bytes"},
                    {"Name": "TransitionEligibleBytes", "Unit": "Bytes"},
                ],
            }]},
            "PrefixGroup": name, "ObjectCount": row["objects"], "Bytes": row["bytes"],
            "StandardBytes": row["storage_class_bytes"]["STANDARD"],
            "GlacierInstantBytes": row["storage_class_bytes"]["GLACIER_IR"],
            "GlacierFlexibleBytes": row["storage_class_bytes"]["GLACIER"],
            "New7DayBytes": row["new_7d_bytes"],
            "TransitionEligibleBytes": row["transition_eligible_bytes"],
        }
        log.info("%s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    del event, context
    now = datetime.now(timezone.utc)
    report = build_report(_s3, BUCKET, now)
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    versioned_key = f"{REPORT_PREFIX}reports/{stamp}.json"
    for key in (versioned_key, f"{REPORT_PREFIX}latest.json"):
        _s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    _emit_metrics(report)
    log.info("storage footprint report: %s", json.dumps({
        "key": versioned_key, "scan": report["scan"], "totals": report["totals"],
    }, sort_keys=True))
    if report["scan"]["truncated"]:
        raise ScanLimitExceeded(str(report["scan"]["reason"]))
    return {"report_key": versioned_key, "scan": report["scan"], "totals": report["totals"]}
