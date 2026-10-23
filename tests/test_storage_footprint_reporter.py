from datetime import datetime, timedelta, timezone

import pytest

from lambdas.storage_footprint_reporter.lambda_function import (
    ScanLimitExceeded,
    _group,
    build_report,
)


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

    assert report["scan"] == {
        "status": "complete", "truncated": False, "reason": None, "pages": 1,
        "estimated_list_cost_usd": 0.000005, "max_objects": 5_000_000,
        "max_list_requests": 6_000,
    }
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


def test_exception_type_is_visible_failure():
    assert issubclass(ScanLimitExceeded, RuntimeError)
