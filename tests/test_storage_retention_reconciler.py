import io
import json
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from lambdas.storage_retention_reconciler.lambda_function import _valid_manifest, reconcile_dataset


class Paginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix, Delimiter=None):
        del Bucket
        keys = sorted(key for key in self.client.objects if key.startswith(Prefix))
        if Delimiter:
            prefixes = sorted({
                Prefix + key[len(Prefix):].split(Delimiter, 1)[0] + Delimiter
                for key in keys if Delimiter in key[len(Prefix):]
            })
            return [{"CommonPrefixes": [{"Prefix": value} for value in prefixes]}]
        return [{"Contents": [self.client.metadata[key] | {"Key": key} for key in keys]}]


class FakeS3:
    def __init__(self, now):
        self.objects = {}
        self.metadata = {}
        self.tags = {}
        self.now = now
        self.put_tag_calls = []

    def add(self, key, value, *, days_old=0, size=None, etag=None):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.objects[key] = body
        self.metadata[key] = {
            "Size": len(body) if size is None else size,
            "LastModified": self.now - timedelta(days=days_old),
            "ETag": etag or f'"{len(self.objects)}"',
        }

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return Paginator(self)

    def get_object(self, Bucket, Key):
        del Bucket
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self.metadata[Key]["ETag"]}

    def get_object_tagging(self, Bucket, Key):
        del Bucket
        return {"TagSet": self.tags.get(Key, [])}

    def put_object_tagging(self, Bucket, Key, Tagging):
        del Bucket
        self.tags[Key] = Tagging["TagSet"]
        self.put_tag_calls.append(Key)


def _complete(s3, prefix, dataset, run_id, *, current=False, days_old=40):
    manifest_key = f"{prefix}runs/{run_id}/manifest.json"
    manifest = {
        "status": "complete", "dataset": dataset, "run_id": run_id,
        "outputs": [{"name": "x", "key": f"{prefix}runs/{run_id}/outputs/x"}],
    }
    s3.add(manifest_key, manifest, days_old=days_old)
    s3.add(f"{prefix}runs/{run_id}/outputs/x", b"x", days_old=days_old, size=200_000)
    if current:
        s3.add(f"{prefix}current.json", {"run_id": run_id, "manifest_key": manifest_key})


def test_audit_classifies_history_current_and_quarantined_orphan_without_tags():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    s3 = FakeS3(now)
    prefix = "pyData/Intermediate/_runs/crsp_daily/"
    _complete(s3, prefix, "crsp_daily", "current", current=True, days_old=1)
    _complete(s3, prefix, "crsp_daily", "old", days_old=40)
    s3.add(f"{prefix}runs/abandoned/outputs/partial", b"partial", days_old=31)
    s3.add(f"{prefix}runs/recent/outputs/partial", b"partial", days_old=2)

    result = reconcile_dataset(s3, "bucket", prefix, mode="audit", now=now)

    assert result["current_objects"] == 2
    assert result["candidate_objects"] == 2
    assert result["candidate_bytes"] == 200_000 + len(json.dumps({
        "status": "complete", "dataset": "crsp_daily", "run_id": "old",
        "outputs": [{"name": "x", "key": f"{prefix}runs/old/outputs/x"}],
    }).encode())
    assert result["orphan_objects"] == 1
    assert result["malformed_runs"] == 0
    assert s3.put_tag_calls == []


def test_apply_tags_only_noncurrent_complete_and_old_incomplete_runs():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    s3 = FakeS3(now)
    prefix = "alternative-data/_runs/gdelt_company_day/"
    _complete(s3, prefix, "gdelt_company_day", "current", current=True, days_old=1)
    _complete(s3, prefix, "gdelt_company_day", "old", days_old=40)
    s3.add(f"{prefix}runs/abandoned/tmp", b"partial", days_old=31)

    result = reconcile_dataset(s3, "bucket", prefix, mode="apply", now=now)

    assert result["tagged_objects"] == 3
    assert all("current" not in key for key in s3.put_tag_calls)
    assert {item["Value"] for tags in s3.tags.values() for item in tags} == {
        "published-history", "orphaned-run"
    }


def test_malformed_current_pointer_fails_closed():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    s3 = FakeS3(now)
    prefix = "pyData/Predictors/_runs/value/"
    _complete(s3, prefix, "value", "old", days_old=40)
    s3.add(f"{prefix}current.json", {"run_id": "old", "manifest_key": "escaped"})

    result = reconcile_dataset(s3, "bucket", prefix, mode="apply", now=now)

    assert result["errors"] == 1
    assert result["malformed_runs"] == 1
    assert s3.put_tag_calls == []


def test_invalid_historical_manifest_is_never_tagged():
    now = datetime(2026, 9, 22, tzinfo=timezone.utc)
    s3 = FakeS3(now)
    prefix = "universe/_runs/universe/"
    _complete(s3, prefix, "universe", "current", current=True, days_old=1)
    s3.add(f"{prefix}runs/bad/manifest.json", {"status": "failed", "dataset": "universe", "run_id": "bad"}, days_old=90)
    s3.add(f"{prefix}runs/bad/outputs/x", b"x", days_old=90)

    result = reconcile_dataset(s3, "bucket", prefix, mode="apply", now=now)

    assert result["malformed_runs"] == 1
    assert s3.put_tag_calls == []


def test_complete_sharded_manifest_is_valid_only_when_all_shards_are_present():
    manifest = {
        "status": "complete",
        "dataset": "option_snapshots",
        "run_id": "2026-09-22",
        "expected_shards": [0, 1],
        "shard_count": 2,
        "shard_manifests": [
            {"shard_index": 0, "manifest_key": "runs/x/shards/s0/manifest.json"},
            {"shard_index": 1, "manifest_key": "runs/x/shards/s1/manifest.json"},
        ],
    }

    assert _valid_manifest(manifest, "option_snapshots", "2026-09-22")
    manifest["shard_manifests"].pop()
    assert not _valid_manifest(manifest, "option_snapshots", "2026-09-22")
