from datetime import datetime, timedelta, timezone

import pytest

from lambdas.storage_footprint_reporter import lambda_function as reporter
from lambdas.storage_footprint_reporter.lambda_function import _group, build_report


class FakeS3:
    def __init__(self, pages):
        self.pages = pages

    def list_objects_v2(self, **kwargs):
        index = int(kwargs.get("ContinuationToken", "0"))
        value = dict(self.pages[index])
        if index + 1 < len(self.pages):
            value.update(IsTruncated=True, NextContinuationToken=str(index + 1))
        else:
            value.setdefault("IsTruncated", False)
        return value


def test_fixed_prefix_grouping_is_low_cardinality():
    assert _group("pyData/Intermediate/x") == "Intermediate"
    assert _group("alternative-data/company-web/direct/raw/x") == "company-web"
    assert _group("alternative-data/_runs/x") == "published alternative-data runs"
    assert _group("control-plane/storage-footprint/latest.json") == "Other"


def test_report_aggregates_classes_growth_eligibility_and_cost():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=2)
    s3 = FakeS3([{"Contents": [
        {"Key": "alternative-data/gdelt/raw/old", "Size": 200_000, "LastModified": old},
        {"Key": "alternative-data/gdelt/raw/recent", "Size": 300_000, "LastModified": recent},
        {"Key": "pyData/Predictors/x", "Size": 400_000, "LastModified": old, "StorageClass": "GLACIER_IR"},
        {"Key": "misc/x", "Size": 50, "LastModified": recent, "StorageClass": "GLACIER"},
    ]}])

    report = build_report(s3, "bucket", now)

    assert report["scan"]["status"] == "complete"
    assert report["scan"]["truncated"] is False
    assert report["scan"]["reason"] is None
    assert report["scan"]["pages"] == 1
    assert report["scan"]["estimated_list_cost_usd"] == 0.000005
    assert report["scan"]["max_objects"] == 5_000_000
    assert report["scan"]["max_list_requests"] == 6_000
    assert report["scan"]["workers"] == 8
    assert report["scan"]["unfinished_prefixes"] == []
    assert report["totals"]["bytes"] == 900_050
    assert report["totals"]["new_7d_standard_bytes"] == 300_000
    assert report["groups"]["GDELT"]["transition_eligible_bytes"] == 200_000
    assert report["totals"]["storage_class_bytes"] == {
        "STANDARD": 500_000, "GLACIER_IR": 400_000, "GLACIER": 50,
    }


def test_scan_caps_mark_report_truncated():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    s3 = FakeS3([{"Contents": [{"Key": "a", "Size": 1, "LastModified": now}]}] * 3)
    report = build_report(s3, "bucket", now, max_pages=2)
    assert report["scan"]["truncated"] is True
    assert report["scan"]["reason"] == "max-list-requests"
    assert report["scan"]["pages"] == 2


class PrefixS3:
    def __init__(self, now):
        self.now = now
        self.puts = []

    def list_objects_v2(self, **kwargs):
        prefix = kwargs.get("Prefix")
        if prefix is None:
            return {
                "Contents": [{"Key": "root.json", "Size": 1, "LastModified": self.now}],
                "CommonPrefixes": [{"Prefix": "a/"}, {"Prefix": "b/"}],
                "IsTruncated": False,
            }
        return {
            "Contents": [{
                "Key": f"{prefix}object", "Size": 10, "LastModified": self.now,
            }],
            "IsTruncated": False,
        }

    def put_object(self, **kwargs):
        self.puts.append(kwargs["Key"])


def test_parallel_prefix_scan_includes_root_objects_exactly_once():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    report = build_report(PrefixS3(now), "bucket", now, max_workers=2)
    assert report["scan"]["status"] == "complete"
    assert report["scan"]["pages"] == 3
    assert report["totals"]["objects"] == 3
    assert report["totals"]["bytes"] == 21


def test_alternative_data_split_counts_direct_and_nested_objects_once():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)

    class NestedS3:
        def list_objects_v2(self, **kwargs):
            prefix = kwargs.get("Prefix")
            if prefix is None:
                return {"CommonPrefixes": [{"Prefix": "alternative-data/"}],
                        "IsTruncated": False}
            if kwargs.get("Delimiter") == "/":
                if prefix == "alternative-data/":
                    return {"Contents": [{"Key": "alternative-data/root", "Size": 1,
                                          "LastModified": now}],
                            "CommonPrefixes": [
                                {"Prefix": "alternative-data/ats/"},
                                {"Prefix": "alternative-data/company-web/"}],
                            "IsTruncated": False}
                if prefix == "alternative-data/company-web/":
                    return {"Contents": [{"Key": "alternative-data/company-web/root",
                                          "Size": 2, "LastModified": now}],
                            "CommonPrefixes": [
                                {"Prefix": "alternative-data/company-web/archive/"},
                                {"Prefix": "alternative-data/company-web/direct/"}],
                            "IsTruncated": False}
            return {"Contents": [{"Key": prefix + "object", "Size": 10,
                                  "LastModified": now}], "IsTruncated": False}

    report = build_report(NestedS3(), "bucket", now)
    assert report["scan"]["status"] == "complete"
    assert report["scan"]["pages"] == 6
    assert report["totals"]["objects"] == 5
    assert report["totals"]["bytes"] == 33
    assert report["groups"]["company-web"]["bytes"] == 22


def test_sharded_raw_and_data_ingress_prefixes_are_disjoint():
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    children = {
        None: ["alternative-data/", "data-ingress/"],
        "alternative-data/": ["alternative-data/ats/"],
        "alternative-data/ats/": ["alternative-data/ats/raw/"],
        "alternative-data/ats/raw/": [
            "alternative-data/ats/raw/00/", "alternative-data/ats/raw/01/"],
        "data-ingress/": ["data-ingress/pit/", "data-ingress/filings/"],
    }

    class ShardedS3:
        def list_objects_v2(self, **kwargs):
            prefix = kwargs.get("Prefix")
            if kwargs.get("Delimiter") == "/":
                return {
                    "Contents": ([{"Key": prefix + "root", "Size": 1,
                                   "LastModified": now}] if prefix else []),
                    "CommonPrefixes": [{"Prefix": child}
                                       for child in children.get(prefix, [])],
                    "IsTruncated": False,
                }
            assert prefix not in children
            return {"Contents": [{"Key": prefix + "object", "Size": 10,
                                  "LastModified": now}], "IsTruncated": False}

    report = build_report(ShardedS3(), "bucket", now)
    assert report["scan"]["status"] == "complete"
    assert report["scan"]["pages"] == 9
    assert report["totals"]["objects"] == 8
    assert report["totals"]["bytes"] == 44


def test_deadline_writes_versioned_diagnostic_without_replacing_latest(monkeypatch):
    now = datetime.now(timezone.utc)
    s3 = PrefixS3(now)
    monkeypatch.setattr(reporter, "_s3", s3)
    monkeypatch.setattr(reporter, "BUCKET", "bucket")
    monkeypatch.setattr(reporter, "REPORT_PREFIX", "reports/")

    class Context:
        @staticmethod
        def get_remaining_time_in_millis():
            return 119_000

    result = reporter.lambda_handler({}, Context())
    assert result["scan"]["status"] == "incomplete"
    assert result["scan"]["reason"] == "deadline-reserve"
    assert len(s3.puts) == 1
    assert s3.puts[0].startswith("reports/reports/")
    assert "reports/latest.json" not in s3.puts
