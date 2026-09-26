import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template


INFRA = Path(__file__).parents[1] / "infra"
sys.path.insert(0, str(INFRA))

from stacks.euclidean_infra_stack import EuclideanInfraStack  # noqa: E402


def _template(*, stage=None):
    context = {"storageRetentionStage": stage} if stage else None
    app = cdk.App(context=context)
    stack = EuclideanInfraStack(
        app, "Test", env=cdk.Environment(account="954976294836", region="us-east-1")
    )
    return Template.from_stack(stack).to_json()


def test_cleanup_lifecycle_rules_are_enabled_by_default_for_production():
    template = _template()
    bucket = next(
        resource for resource in template["Resources"].values()
        if resource["Type"] == "AWS::S3::Bucket"
        and resource["Properties"].get("BucketName") == "euclidean-pipeline-954976294836"
    )
    rules = {row["Id"]: row for row in bucket["Properties"]["LifecycleConfiguration"]["Rules"]}

    assert rules["retain-noncurrent-versions-for-30-days"]["Status"] == "Enabled"
    assert rules["remove-expired-delete-markers"]["Status"] == "Enabled"
    assert rules["expire-xbrl-spill-cache-7d"] == {
        "ExpirationInDays": 7,
        "Id": "expire-xbrl-spill-cache-7d",
        "Prefix": "data-ingress/cache/",
        "Status": "Enabled",
    }
    assert rules["archive-published-history-glacier-instant"]["Status"] == "Enabled"
    assert rules["archive-published-history-glacier-instant"]["ObjectSizeGreaterThan"] == 131072
    assert rules["archive-published-history-glacier-instant"]["Transitions"] == [{
        "StorageClass": "GLACIER_IR", "TransitionInDays": 30,
    }]
    assert rules["archive-pit-raw-30d"]["Transitions"] == [{
        "StorageClass": "GLACIER", "TransitionInDays": 30,
    }]
    assert "data-ingress/filings/" not in {row.get("Prefix") for row in rules.values()}


def test_tagging_stage_keeps_destructive_lifecycle_rules_disabled():
    template = _template(stage="tagging")
    bucket = next(
        resource for resource in template["Resources"].values()
        if resource["Type"] == "AWS::S3::Bucket"
        and resource["Properties"].get("BucketName") == "euclidean-pipeline-954976294836"
    )
    rules = bucket["Properties"]["LifecycleConfiguration"]["Rules"]
    staged = [rule for rule in rules if rule["Id"] not in {
        "retain-noncurrent-versions-for-30-days", "remove-expired-delete-markers",
    }]
    assert staged and all(rule["Status"] == "Disabled" for rule in staged)


def test_reconciler_applies_tags_and_has_no_delete_permission():
    template = _template()
    function = next(
        resource for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "euclidean-storage-retention-reconciler"
    )
    assert function["Properties"]["Environment"]["Variables"]["RETENTION_MODE"] == "apply"
    assert function["Properties"]["ReservedConcurrentExecutions"] == 1

    actions = []
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::Policy":
            continue
        for statement in resource["Properties"]["PolicyDocument"]["Statement"]:
            value = statement.get("Action", [])
            actions.extend(value if isinstance(value, list) else [value])
    assert "s3:DeleteObject" not in actions
    assert "s3:PutObjectTagging" in actions
