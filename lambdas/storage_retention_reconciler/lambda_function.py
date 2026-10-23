"""Audit and tag immutable dataset runs for S3 lifecycle management."""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger()
log.setLevel(logging.INFO)
BUCKET = os.environ.get("S3_BUCKET", "euclidean-pipeline-954976294836")
MODE = os.environ.get("RETENTION_MODE", "audit").strip().lower()
RUN_ROOTS = tuple(filter(None, os.environ.get("RUN_ROOTS", "pyData/Intermediate/_runs/,pyData/Predictors/_runs/,alternative-data/_runs/,universe/_runs/").split(",")))
ORPHAN_QUARANTINE_DAYS = int(os.environ.get("ORPHAN_QUARANTINE_DAYS", "30"))
MAX_ISSUES = int(os.environ.get("MAX_ISSUES", "100"))
RETENTION_TAG = "euclidean-retention"
ARCHIVE_VALUE = "published-history"
ORPHAN_VALUE = "orphaned-run"
_s3 = boto3.client("s3", config=Config(max_pool_connections=64))


class ScanCounter:
    """Thread-safe S3 LIST page count for one reconciliation."""
    def __init__(self) -> None:
        self.pages = 0
        self._lock = Lock()

    def add_page(self) -> None:
        with self._lock:
            self.pages += 1


def _missing(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound")


def _get_json(s3: Any, bucket: str, key: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _missing(exc):
            return None, None
        raise
    try:
        value = json.loads(response["Body"].read())
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid-json:{key}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"json-object-required:{key}")
    return value, response.get("ETag")


def _common_prefixes(s3: Any, bucket: str, prefix: str, scan: ScanCounter | None = None) -> list[str]:
    values = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        if scan:
            scan.add_page()
        values.extend(item["Prefix"] for item in page.get("CommonPrefixes", []))
    return values


def _objects(s3: Any, bucket: str, prefix: str, scan: ScanCounter | None = None) -> list[dict[str, Any]]:
    values = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        if scan:
            scan.add_page()
        values.extend(item for item in page.get("Contents", []) if item.get("Key"))
    return values


def _merge_tag(s3: Any, bucket: str, key: str, value: str) -> bool:
    existing = s3.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", [])
    tags = {item["Key"]: item["Value"] for item in existing}
    if tags.get(RETENTION_TAG) == value:
        return False
    tags[RETENTION_TAG] = value
    s3.put_object_tagging(Bucket=bucket, Key=key, Tagging={"TagSet": [
        {"Key": name, "Value": tags[name]} for name in sorted(tags)
    ]})
    return True


def _valid_manifest(manifest: dict[str, Any], dataset: str, run_id: str) -> bool:
    if not (manifest.get("status") == "complete" and str(manifest.get("dataset")) == dataset and str(manifest.get("run_id")) == run_id):
        return False
    if manifest.get("outputs") or manifest.get("output"):
        return True
    shards = manifest.get("shard_manifests")
    expected = manifest.get("expected_shards")
    if not isinstance(shards, list) or not shards or not isinstance(expected, list) or len(expected) != len(shards):
        return False
    indexes = [item.get("shard_index") for item in shards if isinstance(item, dict)]
    keys = [item.get("manifest_key") for item in shards if isinstance(item, dict)]
    return (
        manifest.get("shard_count") == len(shards)
        and len(indexes) == len(shards)
        and all(isinstance(index, int) for index in indexes + expected)
        and set(indexes) == set(expected)
        and len(set(keys)) == len(shards)
        and all(isinstance(key, str) and key for key in keys)
    )


def _run_id(dataset_prefix: str, run_prefix: str) -> str:
    base = f"{dataset_prefix}runs/"
    if not run_prefix.startswith(base):
        raise ValueError(f"run-prefix-escaped:{run_prefix}")
    return run_prefix[len(base):].rstrip("/")


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in (
        "candidate_objects", "candidate_bytes", "candidate_standard_bytes",
        "candidate_glacier_ir_bytes", "candidate_glacier_bytes", "tagged_objects",
        "newly_tagged_bytes", "orphan_objects", "orphan_bytes", "malformed_runs",
        "malformed_run_bytes", "errors", "current_objects", "current_bytes", "list_requests",
    )}


def _storage_bytes(objects: list[dict[str, Any]]) -> dict[str, int]:
    values = {"standard": 0, "glacier_ir": 0, "glacier": 0}
    for item in objects:
        size = int(item.get("Size", 0))
        storage_class = str(item.get("StorageClass") or "STANDARD").upper()
        if storage_class == "GLACIER_IR":
            values["glacier_ir"] += size
        elif storage_class in {"GLACIER", "DEEP_ARCHIVE"}:
            values["glacier"] += size
        else:
            values["standard"] += size
    return values


def _issue(dataset: str, run_id: str, reason: str, objects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    objects = objects or []
    return {"dataset": dataset, "run_id": run_id, "reason": reason,
            "object_count": len(objects), "bytes": sum(int(item.get("Size", 0)) for item in objects)}


def reconcile_dataset(s3: Any, bucket: str, dataset_prefix: str, *, mode: str,
                      now: datetime, scan: ScanCounter | None = None) -> dict[str, Any]:
    counts = _empty_counts()
    issues: list[dict[str, Any]] = []
    dataset = dataset_prefix.rstrip("/").rsplit("/", 1)[-1]
    current_key = f"{dataset_prefix}current.json"
    current_run_id = None
    current_etag = None
    pointer_missing = False
    try:
        pointer, current_etag = _get_json(s3, bucket, current_key)
        if pointer is None:
            pointer_missing = True
        else:
            current_run_id = str(pointer.get("run_id") or "")
            expected_manifest = f"{dataset_prefix}runs/{current_run_id}/manifest.json"
            if not current_run_id or pointer.get("manifest_key") != expected_manifest:
                raise ValueError("invalid-current-pointer")
            manifest, _ = _get_json(s3, bucket, expected_manifest)
            if manifest is None or not _valid_manifest(manifest, dataset, current_run_id):
                raise ValueError("invalid-current-manifest")
    except (ClientError, ValueError, KeyError, TypeError) as exc:
        counts["errors"] = counts["malformed_runs"] = 1
        reason = str(exc) if isinstance(exc, ValueError) else "current-read-error"
        return {**counts, "issues": [_issue(dataset, current_run_id or "__current__", reason)]}

    candidates: list[tuple[list[dict[str, Any]], str]] = []
    cutoff = now - timedelta(days=ORPHAN_QUARANTINE_DAYS)
    run_prefixes = sorted(_common_prefixes(s3, bucket, f"{dataset_prefix}runs/", scan))

    def load_run(run_prefix: str):
        try:
            objects = _objects(s3, bucket, run_prefix, scan)
            manifest = _get_json(s3, bucket, f"{run_prefix}manifest.json")[0]
            return run_prefix, objects, manifest, False
        except (ClientError, ValueError, TypeError):
            return run_prefix, [], None, True

    noncurrent = [prefix for prefix in run_prefixes if _run_id(dataset_prefix, prefix) != current_run_id]
    with ThreadPoolExecutor(max_workers=min(32, max(1, len(noncurrent)))) as pool:
        loaded = {prefix: (objects, manifest, malformed)
                  for prefix, objects, manifest, malformed in pool.map(load_run, noncurrent)}

    for run_prefix in run_prefixes:
        run_id = _run_id(dataset_prefix, run_prefix)
        if run_id == current_run_id:
            objects = _objects(s3, bucket, run_prefix, scan)
            counts["current_objects"] += len(objects)
            counts["current_bytes"] += sum(int(item.get("Size", 0)) for item in objects)
            continue
        objects, manifest, malformed = loaded[run_prefix]
        object_bytes = sum(int(item.get("Size", 0)) for item in objects)
        if malformed:
            counts["malformed_runs"] += 1
            issues.append(_issue(dataset, run_id, "run-read-error", objects))
        elif manifest is not None and pointer_missing:
            counts["malformed_runs"] += 1
            counts["malformed_run_bytes"] += object_bytes
            issues.append(_issue(dataset, run_id, "missing-current-pointer", objects))
        elif manifest is not None and not _valid_manifest(manifest, dataset, run_id):
            counts["malformed_runs"] += 1
            counts["malformed_run_bytes"] += object_bytes
            issues.append(_issue(dataset, run_id, "invalid-historical-manifest", objects))
        elif manifest is not None:
            storage = _storage_bytes(objects)
            counts["candidate_objects"] += len(objects)
            counts["candidate_bytes"] += sum(storage.values())
            counts["candidate_standard_bytes"] += storage["standard"]
            counts["candidate_glacier_ir_bytes"] += storage["glacier_ir"]
            counts["candidate_glacier_bytes"] += storage["glacier"]
            candidates.append((objects, ARCHIVE_VALUE))
        else:
            newest = max((item.get("LastModified") for item in objects), default=None)
            if newest is not None and newest <= cutoff:
                counts["orphan_objects"] += len(objects)
                counts["orphan_bytes"] += object_bytes
                candidates.append((objects, ORPHAN_VALUE))

    if mode != "apply" or not candidates:
        return {**counts, "issues": issues[:MAX_ISSUES]}
    _, final_etag = _get_json(s3, bucket, current_key)
    if final_etag != current_etag:
        counts["errors"] += 1
        issues.append(_issue(dataset, current_run_id or "__current__", "current-pointer-changed"))
        return {**counts, "issues": issues[:MAX_ISSUES]}

    tag_jobs = [(item["Key"], value, int(item.get("Size", 0)))
                for objects, value in candidates for item in objects]

    def tag_object(job: tuple[str, str, int]) -> tuple[int, int, int]:
        key, value, size = job
        try:
            tagged = _merge_tag(s3, bucket, key, value)
            return (1 if tagged else 0, size if tagged else 0, 0)
        except ClientError:
            log.exception("failed to tag retention candidate")
            return 0, 0, 1

    with ThreadPoolExecutor(max_workers=min(32, max(1, len(tag_jobs)))) as pool:
        for tagged, tagged_bytes, errors in pool.map(tag_object, tag_jobs):
            counts["tagged_objects"] += tagged
            counts["newly_tagged_bytes"] += tagged_bytes
            counts["errors"] += errors
    return {**counts, "issues": issues[:MAX_ISSUES]}


def _emit_metrics(values: dict[str, int], duration_ms: float, root: str) -> None:
    metric_map = {
        "candidate_objects": ("CandidateObjects", "Count"), "candidate_bytes": ("CandidateBytes", "Bytes"),
        "candidate_standard_bytes": ("CandidateStandardBytes", "Bytes"),
        "candidate_glacier_ir_bytes": ("CandidateGlacierInstantBytes", "Bytes"),
        "candidate_glacier_bytes": ("CandidateGlacierFlexibleBytes", "Bytes"),
        "tagged_objects": ("TaggedObjects", "Count"), "newly_tagged_bytes": ("NewlyTaggedBytes", "Bytes"),
        "orphan_objects": ("OrphanObjects", "Count"), "orphan_bytes": ("OrphanBytes", "Bytes"),
        "malformed_runs": ("MalformedRuns", "Count"), "malformed_run_bytes": ("MalformedRunBytes", "Bytes"),
        "errors": ("Errors", "Count"), "current_bytes": ("CurrentBytes", "Bytes"),
        "list_requests": ("ListRequests", "Count"),
    }
    payload: dict[str, Any] = {"_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
        "Namespace": "Euclidean/StorageLifecycle", "Dimensions": [["Mode", "Root"]],
        "Metrics": [{"Name": name, "Unit": unit} for name, unit in metric_map.values()]
        + [{"Name": "DurationMs", "Unit": "Milliseconds"}],
    }]}, "Mode": MODE, "Root": root, "DurationMs": duration_ms}
    payload.update({name: values[key] for key, (name, _) in metric_map.items()})
    log.info("%s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    del event, context
    if MODE not in {"audit", "apply"}:
        raise ValueError(f"invalid RETENTION_MODE:{MODE}")
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    totals = _empty_counts()
    roots = []
    all_issues: list[dict[str, Any]] = []
    for root in RUN_ROOTS:
        root_started = time.monotonic()
        root_counts = _empty_counts()
        scan = ScanCounter()
        root_issues: list[dict[str, Any]] = []
        for dataset_prefix in _common_prefixes(_s3, BUCKET, root, scan):
            result = reconcile_dataset(_s3, BUCKET, dataset_prefix, mode=MODE, now=now, scan=scan)
            for key in root_counts:
                if key != "list_requests":
                    root_counts[key] += int(result[key])
            root_issues.extend(result.get("issues", []))
        root_counts["list_requests"] = scan.pages
        for key, value in root_counts.items():
            totals[key] += value
        bounded = [dict(root=root, **issue) for issue in root_issues[:MAX_ISSUES]]
        all_issues.extend(bounded)
        root_result = {"root": root, **root_counts, "issues": bounded}
        roots.append(root_result)
        log.info("storage retention root: %s", json.dumps(root_result, sort_keys=True))
        _emit_metrics(root_counts, (time.monotonic() - root_started) * 1000, root)
    log.info("storage retention issues: %s", json.dumps({
        "issue_count": len(all_issues),
        "issues_truncated": sum(item["malformed_runs"] for item in roots) > len(all_issues),
        "issues": all_issues[:MAX_ISSUES],
    }, sort_keys=True))
    return {"mode": MODE, "bucket": BUCKET, "duration_ms": (time.monotonic() - started) * 1000,
            "roots": roots, "issues": all_issues[:MAX_ISSUES], **totals}
