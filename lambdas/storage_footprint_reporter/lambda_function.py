"""Bounded weekly S3 storage-footprint report using object metadata only."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _empty_groups() -> dict[str, dict[str, Any]]:
    groups = {name: _empty_group() for _, name in GROUPS}
    groups["Other"] = _empty_group()
    return groups


def _record_object(
    groups: dict[str, dict[str, Any]], item: dict[str, Any],
    cutoff_7d: datetime, cutoff_30d: datetime,
) -> None:
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
            and modified <= cutoff_30d
            and any(key.startswith(prefix) for prefix in RAW_PREFIXES)):
        row["transition_eligible_objects"] += 1
        row["transition_eligible_bytes"] += size


def _merge_groups(
    target: dict[str, dict[str, Any]], source: dict[str, dict[str, Any]],
) -> None:
    for name, row in source.items():
        output = target[name]
        for key in (
            "objects", "bytes", "new_7d_objects", "new_7d_bytes",
            "new_7d_standard_bytes", "transition_eligible_objects",
            "transition_eligible_bytes",
        ):
            output[key] += row[key]
        for storage, value in row["storage_class_bytes"].items():
            output["storage_class_bytes"][storage] += value


def build_report(s3: Any, bucket: str, now: datetime, *, max_objects: int = MAX_OBJECTS,
                 max_pages: int = MAX_PAGES, max_workers: int = 8,
                 remaining_time_ms: Any | None = None) -> dict[str, Any]:
    started = time.monotonic()
    groups = _empty_groups()
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    state = {"pages": 0, "objects": 0, "reason": None}
    lock = threading.Lock()
    stopped = threading.Event()

    def stop(reason: str) -> None:
        with lock:
            state["reason"] = state["reason"] or reason
        stopped.set()

    def reserve_page() -> bool:
        if remaining_time_ms is not None and remaining_time_ms() <= 120_000:
            stop("deadline-reserve")
            return False
        with lock:
            if state["pages"] >= max_pages:
                state["reason"] = state["reason"] or "max-list-requests"
                stopped.set()
                return False
            state["pages"] += 1
            return True

    def accept_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        with lock:
            remaining = max_objects - state["objects"]
            accepted = items[:max(0, remaining)]
            state["objects"] += len(accepted)
            if len(accepted) < len(items) or state["objects"] >= max_objects:
                state["reason"] = state["reason"] or "max-objects"
                stopped.set()
            return accepted

    # Delimiter discovery yields disjoint top-level prefixes and any root keys.
    prefixes: set[str] = set()
    discovery_token = None
    while not stopped.is_set():
        if not reserve_page():
            break
        args = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 1000}
        if discovery_token:
            args["ContinuationToken"] = discovery_token
        page = s3.list_objects_v2(**args)
        for item in accept_items(list(page.get("Contents", []))):
            _record_object(groups, item, cutoff_7d, cutoff_30d)
        prefixes.update(
            str(item.get("Prefix") or "")
            for item in page.get("CommonPrefixes", []) if item.get("Prefix")
        )
        if not page.get("IsTruncated"):
            break
        discovery_token = page.get("NextContinuationToken")
        if not discovery_token:
            stop("missing-continuation-token")
            break

    completed: set[str] = set()
    errors: list[dict[str, str]] = []

    def scan_prefix(prefix: str) -> tuple[str, dict[str, dict[str, Any]], bool]:
        local = _empty_groups()
        token = None
        try:
            while not stopped.is_set():
                if not reserve_page():
                    break
                args = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
                if token:
                    args["ContinuationToken"] = token
                page = s3.list_objects_v2(**args)
                for item in accept_items(list(page.get("Contents", []))):
                    _record_object(local, item, cutoff_7d, cutoff_30d)
                if stopped.is_set():
                    break
                if not page.get("IsTruncated"):
                    return prefix, local, True
                token = page.get("NextContinuationToken")
                if not token:
                    stop("missing-continuation-token")
                    break
        except Exception as exc:  # preserve a safe partial report instead of async retrying
            with lock:
                errors.append({"prefix": prefix, "error_type": type(exc).__name__})
            stop("list-error")
        return prefix, local, False

    if prefixes and not stopped.is_set():
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(scan_prefix, prefix) for prefix in sorted(prefixes)]
            for future in as_completed(futures):
                prefix, local, complete = future.result()
                _merge_groups(groups, local)
                if complete:
                    completed.add(prefix)

    totals = _empty_group()
    for row in groups.values():
        for key in ("objects", "bytes", "new_7d_objects", "new_7d_bytes",
                    "new_7d_standard_bytes", "transition_eligible_objects", "transition_eligible_bytes"):
            totals[key] += row[key]
        for storage, value in row["storage_class_bytes"].items():
            totals["storage_class_bytes"][storage] += value
    return {
        "schema_version": 2,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "bucket": bucket,
        "scan": {
            "status": "incomplete" if state["reason"] else "complete",
            "truncated": bool(state["reason"]),
            "reason": state["reason"],
            "pages": state["pages"],
            "estimated_list_cost_usd": round(
                state["pages"] / 1000 * LIST_COST_PER_1000, 6
            ),
            "max_objects": max_objects,
            "max_list_requests": max_pages,
            "workers": max_workers,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "unfinished_prefixes": sorted(prefixes - completed),
            "errors": errors,
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
    scan = report["scan"]
    log.info("%s", json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "Euclidean/StorageFootprint", "Dimensions": [[]],
            "Metrics": [
                {"Name": "ScanComplete", "Unit": "Count"},
                {"Name": "UnfinishedPrefixCount", "Unit": "Count"},
                {"Name": "ListRequests", "Unit": "Count"},
                {"Name": "ScanDurationMs", "Unit": "Milliseconds"},
            ],
        }]},
        "ScanComplete": int(scan["status"] == "complete"),
        "UnfinishedPrefixCount": len(scan["unfinished_prefixes"]),
        "ListRequests": scan["pages"],
        "ScanDurationMs": scan["duration_ms"],
    }, sort_keys=True, separators=(",", ":")))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    del event
    now = datetime.now(timezone.utc)
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    report = build_report(_s3, BUCKET, now, remaining_time_ms=remaining)
    body = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
    versioned_key = f"{REPORT_PREFIX}reports/{stamp}.json"
    keys = [versioned_key]
    if report["scan"]["status"] == "complete":
        keys.append(f"{REPORT_PREFIX}latest.json")
    for key in keys:
        _s3.put_object(Bucket=BUCKET, Key=key, Body=body, ContentType="application/json")
    _emit_metrics(report)
    log.info("storage footprint report: %s", json.dumps({
        "key": versioned_key, "scan": report["scan"], "totals": report["totals"],
    }, sort_keys=True))
    return {"report_key": versioned_key, "scan": report["scan"], "totals": report["totals"]}
