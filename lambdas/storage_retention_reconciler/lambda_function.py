"""Audit and tag immutable dataset runs for S3 lifecycle management."""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


log = logging.getLogger()
log.setLevel(logging.INFO)

BUCKET = os.environ.get("S3_BUCKET", "euclidean-pipeline-954976294836")
MODE = os.environ.get("RETENTION_MODE", "audit").strip().lower()
RUN_ROOTS = tuple(filter(None, os.environ.get(
    "RUN_ROOTS",
    "pyData/Intermediate/_runs/,pyData/Predictors/_runs/,alternative-data/_runs/,universe/_runs/",
).split(",")))
ORPHAN_QUARANTINE_DAYS = int(os.environ.get("ORPHAN_QUARANTINE_DAYS", "30"))
RETENTION_TAG = "euclidean-retention"
ARCHIVE_VALUE = "published-history"
ORPHAN_VALUE = "orphaned-run"

_s3 = boto3.client("s3", config=Config(max_pool_connections=64))


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
        raise ValueError(f"invalid JSON: {key}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {key}")
    return value, response.get("ETag")


def _common_prefixes(s3: Any, bucket: str, prefix: str) -> list[str]:
    values = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        values.extend(item["Prefix"] for item in page.get("CommonPrefixes", []))
    return values


def _objects(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    values = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        values.extend(item for item in page.get("Contents", []) if item.get("Key"))
    return values


def _merge_tag(s3: Any, bucket: str, key: str, value: str) -> bool:
    existing = s3.get_object_tagging(Bucket=bucket, Key=key).get("TagSet", [])
    tags = {item["Key"]: item["Value"] for item in existing}
    if tags.get(RETENTION_TAG) == value:
        return False
    tags[RETENTION_TAG] = value
    s3.put_object_tagging(
        Bucket=bucket,
        Key=key,
        Tagging={"TagSet": [{"Key": name, "Value": tags[name]} for name in sorted(tags)]},
    )
    return True


def _valid_manifest(manifest: dict[str, Any], dataset: str, run_id: str) -> bool:
    if not (
        manifest.get("status") == "complete"
        and str(manifest.get("dataset")) == dataset
        and str(manifest.get("run_id")) == run_id
    ):
        return False
    if manifest.get("outputs") or manifest.get("output"):
        return True

    # Some large datasets publish a complete top-level manifest whose outputs
    # live in independently validated shard manifests.
    shard_manifests = manifest.get("shard_manifests")
    expected_shards = manifest.get("expected_shards")
    if not isinstance(shard_manifests, list) or not shard_manifests:
        return False
    if not isinstance(expected_shards, list) or len(expected_shards) != len(shard_manifests):
        return False
    if manifest.get("shard_count") != len(shard_manifests):
        return False
    indexes = [item.get("shard_index") for item in shard_manifests if isinstance(item, dict)]
    keys = [item.get("manifest_key") for item in shard_manifests if isinstance(item, dict)]
    return (
        len(indexes) == len(shard_manifests)
        and all(isinstance(index, int) for index in indexes)
        and all(isinstance(index, int) for index in expected_shards)
        and set(indexes) == set(expected_shards)
        and len(set(keys)) == len(shard_manifests)
        and all(isinstance(key, str) and key for key in keys)
    )


def _run_id(dataset_prefix: str, run_prefix: str) -> str:
    base = f"{dataset_prefix}runs/"
    if not run_prefix.startswith(base):
        raise ValueError(f"run prefix escaped dataset root: {run_prefix}")
    return run_prefix[len(base):].rstrip("/")


def _empty_counts() -> dict[str, int]:
    return {
        "candidate_objects": 0,
        "candidate_bytes": 0,
        "tagged_objects": 0,
        "orphan_objects": 0,
        "orphan_bytes": 0,
        "malformed_runs": 0,
        "errors": 0,
        "current_objects": 0,
    }


def reconcile_dataset(
    s3: Any,
    bucket: str,
    dataset_prefix: str,
    *,
    mode: str,
    now: datetime,
) -> dict[str, int]:
    counts = _empty_counts()
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
                raise ValueError(f"invalid current pointer: {current_key}")
            manifest, _ = _get_json(s3, bucket, expected_manifest)
            if manifest is None or not _valid_manifest(manifest, dataset, current_run_id):
                raise ValueError(f"invalid current manifest: {expected_manifest}")
    except (ClientError, ValueError, KeyError, TypeError):
        counts["errors"] += 1
        counts["malformed_runs"] += 1
        return counts

    candidates: list[tuple[list[dict[str, Any]], str]] = []
    cutoff = now - timedelta(days=ORPHAN_QUARANTINE_DAYS)
    runs_root = f"{dataset_prefix}runs/"
    run_prefixes = sorted(_common_prefixes(s3, bucket, runs_root))

    def load_run(run_prefix: str):
        try:
            objects = _objects(s3, bucket, run_prefix)
            manifest = _get_json(s3, bucket, f"{run_prefix}manifest.json")[0]
            return run_prefix, objects, manifest, False
        except (ClientError, ValueError, TypeError):
            return run_prefix, [], None, True

    noncurrent = [
        prefix for prefix in run_prefixes
        if _run_id(dataset_prefix, prefix) != current_run_id
    ]
    workers = min(32, max(1, len(noncurrent)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        loaded = {
            prefix: (objects, manifest, malformed)
            for prefix, objects, manifest, malformed in pool.map(load_run, noncurrent)
        }

    for run_prefix in run_prefixes:
        run_id = _run_id(dataset_prefix, run_prefix)
        if run_id == current_run_id:
            objects = _objects(s3, bucket, run_prefix)
            counts["current_objects"] += len(objects)
            continue
        objects, manifest, malformed = loaded[run_prefix]
        if malformed:
            counts["malformed_runs"] += 1
            continue
        if manifest is not None:
            if pointer_missing:
                counts["malformed_runs"] += 1
                continue
            if not _valid_manifest(manifest, dataset, run_id):
                counts["malformed_runs"] += 1
                continue
            counts["candidate_objects"] += len(objects)
            counts["candidate_bytes"] += sum(int(item.get("Size", 0)) for item in objects)
            candidates.append((objects, ARCHIVE_VALUE))
            continue
        newest = max((item.get("LastModified") for item in objects), default=None)
        if newest is not None and newest <= cutoff:
            counts["orphan_objects"] += len(objects)
            counts["orphan_bytes"] += sum(int(item.get("Size", 0)) for item in objects)
            candidates.append((objects, ORPHAN_VALUE))

    if mode != "apply" or not candidates:
        return counts

    # A promotion while the dataset was being classified invalidates the
    # snapshot. Skip the whole dataset and let the next daily run retry.
    _, final_etag = _get_json(s3, bucket, current_key)
    if final_etag != current_etag:
        counts["errors"] += 1
        return counts
    tag_jobs = [
        (item["Key"], value)
        for objects, value in candidates
        for item in objects
    ]

    # Tagging requires a read/merge/write cycle per object. Run those independent
    # I/O operations concurrently so a large historical backlog cannot exhaust
    # Lambda's 15-minute ceiling. The client connection pool is sized for this
    # bound, and failures are counted so lifecycle activation can fail closed.
    def tag_object(job: tuple[str, str]) -> tuple[int, int]:
        key, value = job
        try:
            return (1 if _merge_tag(s3, bucket, key, value) else 0, 0)
        except ClientError:
            log.exception("failed to tag retention candidate: %s", key)
            return 0, 1

    workers = min(32, max(1, len(tag_jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tagged, errors in pool.map(tag_object, tag_jobs):
            counts["tagged_objects"] += tagged
            counts["errors"] += errors
    return counts


def _emit_metrics(totals: dict[str, int], duration_ms: float) -> None:
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "Euclidean/StorageLifecycle",
                "Dimensions": [["Mode"]],
                "Metrics": [
                    {"Name": "CandidateObjects", "Unit": "Count"},
                    {"Name": "CandidateBytes", "Unit": "Bytes"},
                    {"Name": "TaggedObjects", "Unit": "Count"},
                    {"Name": "OrphanObjects", "Unit": "Count"},
                    {"Name": "OrphanBytes", "Unit": "Bytes"},
                    {"Name": "MalformedRuns", "Unit": "Count"},
                    {"Name": "Errors", "Unit": "Count"},
                    {"Name": "DurationMs", "Unit": "Milliseconds"},
                ],
            }],
        },
        "Mode": MODE,
        "CandidateObjects": totals["candidate_objects"],
        "CandidateBytes": totals["candidate_bytes"],
        "TaggedObjects": totals["tagged_objects"],
        "OrphanObjects": totals["orphan_objects"],
        "OrphanBytes": totals["orphan_bytes"],
        "MalformedRuns": totals["malformed_runs"],
        "Errors": totals["errors"],
        "DurationMs": duration_ms,
    }
    log.info("%s", json.dumps(payload, sort_keys=True, separators=(",", ":")))


def lambda_handler(event: dict[str, Any] | None, context: Any) -> dict[str, Any]:
    del event, context
    if MODE not in {"audit", "apply"}:
        raise ValueError(f"invalid RETENTION_MODE: {MODE}")
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    totals = _empty_counts()
    roots = []
    for root in RUN_ROOTS:
        root_counts = _empty_counts()
        for dataset_prefix in _common_prefixes(_s3, BUCKET, root):
            values = reconcile_dataset(
                _s3, BUCKET, dataset_prefix, mode=MODE, now=now
            )
            for key, value in values.items():
                root_counts[key] += value
                totals[key] += value
        roots.append({"root": root, **root_counts})
        log.info("storage retention root: %s", json.dumps(roots[-1], sort_keys=True))
    duration_ms = (time.monotonic() - started) * 1000
    _emit_metrics(totals, duration_ms)
    return {"mode": MODE, "bucket": BUCKET, "roots": roots, **totals}
