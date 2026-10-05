"""Release only the month-end state machine and its RunTask permission.

This intentionally starts from the *deployed* CloudFormation template. The
CDK synthesis currently includes unrelated retention drift; copying its whole
template would release that drift alongside the predictor readiness change.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


STACK = "euclidean-infra"
BUCKET = "euclidean-pipeline-954976294836"
DEPLOY_ROLE = (
    "arn:aws:iam::954976294836:role/"
    "cdk-hnb659fds-deploy-role-954976294836-us-east-1"
)
RESOURCE_PROPERTIES = {
    "Pipeline": "DefinitionString",
    "StepFunctionRoleC4BAB6F8": "Policies",
}
HIGH_MEMORY_TASK_ARN = (
    "arn:aws:ecs:us-east-1:954976294836:task-definition/"
    "euclidean-monthly-predictor-high-memory:*"
)


def _template(value: object) -> dict:
    return json.loads(value) if isinstance(value, str) else value


def _deployment_session() -> boto3.Session:
    credentials = boto3.client("sts", region_name="us-east-1").assume_role(
        RoleArn=DEPLOY_ROLE, RoleSessionName="ScopedMonthlyRelease",
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name="us-east-1",
    )


def _canonical_policies(policies: list[dict]) -> list[dict]:
    """Compare IAM permissions, not CloudFormation's ordering of their lists."""
    normalized = copy.deepcopy(policies)
    for policy in normalized:
        statements = policy["PolicyDocument"]["Statement"]
        for statement in statements:
            for field in ("Action", "Resource"):
                if isinstance(statement.get(field), list):
                    statement[field].sort()
        statements.sort(key=lambda item: json.dumps(item, sort_keys=True))
    return sorted(normalized, key=lambda item: item.get("PolicyName", ""))


def scoped_template(current: dict, synthesized: dict) -> dict:
    result = copy.deepcopy(current)
    old_policies = current["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"]
    synthesized_policies = copy.deepcopy(
        synthesized["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"]
    )
    new_policies = copy.deepcopy(synthesized_policies)
    replacements = 0
    for policy in new_policies:
        for statement in policy["PolicyDocument"]["Statement"]:
            resources = statement.get("Resource")
            if isinstance(resources, list) and HIGH_MEMORY_TASK_ARN in resources:
                if statement.get("Action") != "ecs:RunTask":
                    raise RuntimeError("High-memory task ARN appears outside RunTask permission")
                resources.remove(HIGH_MEMORY_TASK_ARN)
                replacements += 1
    already_authorized = _canonical_policies(synthesized_policies) == _canonical_policies(old_policies)
    only_expected_addition = (
        replacements == 1
        and _canonical_policies(new_policies) == _canonical_policies(old_policies)
    )
    if not (already_authorized or only_expected_addition):
        raise RuntimeError("Monthly release changes IAM beyond the exact high-memory task ARN")
    for resource, property_name in RESOURCE_PROPERTIES.items():
        old = current["Resources"][resource]
        new = synthesized["Resources"][resource]
        if old["Type"] != new["Type"]:
            raise RuntimeError(f"{resource} changes resource type")
        result["Resources"][resource]["Properties"][property_name] = copy.deepcopy(
            old_policies if resource == "StepFunctionRoleC4BAB6F8" and already_authorized
            else new["Properties"][property_name]
        )
    changed = {
        resource for resource in current["Resources"]
        if current["Resources"][resource] != result["Resources"][resource]
    }
    if not changed.issubset(RESOURCE_PROPERTIES):
        raise RuntimeError(f"Unexpected scoped resource changes: {sorted(changed)}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--synthesized-template",
        type=Path,
        default=Path("cdk.out/EuclideanInfra.template.json"),
    )
    args = parser.parse_args()
    local_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if args.sha != local_sha or len(args.sha) != 40:
        raise RuntimeError("Release SHA must equal the exact local commit")
    if args.execute:
        remote_sha = subprocess.check_output(
            ["git", "ls-remote", "origin", "refs/heads/main"], text=True
        ).split()[0]
        if remote_sha != args.sha:
            raise RuntimeError("Scoped release requires the exact current origin/main SHA")

    deployment = _deployment_session()
    cfn = deployment.client("cloudformation")
    current = _template(cfn.get_template(StackName=STACK, TemplateStage="Processed")["TemplateBody"])
    synthesized = json.loads(args.synthesized_template.read_text(encoding="utf-8"))
    desired = scoped_template(current, synthesized)
    body = json.dumps(desired, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    print(f"Scoped monthly template sha256={digest}; resources={sorted(RESOURCE_PROPERTIES)}")
    if not args.execute:
        return

    key = f"data-ingress/deployments/monthly-readiness/{args.sha}/{digest}.json"
    s3 = deployment.client("s3")
    try:
        s3.put_object(
            Bucket=BUCKET, Key=key, Body=body, ContentType="application/json",
            ServerSideEncryption="AES256", IfNoneMatch="*",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
            raise
        existing = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise RuntimeError("Immutable deployment template collision") from error

    name = f"november-readiness-{args.sha[:12]}-{int(time.time())}"
    url = f"https://{BUCKET}.s3.us-east-1.amazonaws.com/{key}"
    response = cfn.create_change_set(
        StackName=STACK, ChangeSetName=name, ChangeSetType="UPDATE",
        TemplateURL=url, Capabilities=["CAPABILITY_NAMED_IAM"],
        Parameters=[{"ParameterKey": "BootstrapVersion", "UsePreviousValue": True}],
        Description=f"Scoped November readiness from main {args.sha}",
    )
    change_set_id = response["Id"]
    while True:
        detail = cfn.describe_change_set(ChangeSetName=change_set_id, StackName=STACK)
        if detail["Status"] in {"CREATE_COMPLETE", "FAILED"}:
            break
        time.sleep(3)
    if detail["Status"] == "FAILED":
        if "didn't contain changes" in detail.get("StatusReason", ""):
            print("Scoped monthly release already complete")
            return
        raise RuntimeError(detail.get("StatusReason", "Change-set creation failed"))

    changes = detail.get("Changes", [])
    actual = {item["ResourceChange"]["LogicalResourceId"] for item in changes}
    expected = {
        resource for resource in RESOURCE_PROPERTIES
        if current["Resources"][resource] != desired["Resources"][resource]
    }
    if actual != expected:
        raise RuntimeError(f"Refusing non-scoped CloudFormation changes: {sorted(actual)}")
    for item in changes:
        change = item["ResourceChange"]
        if change["Action"] != "Modify" or change.get("Replacement") not in {"False", False}:
            raise RuntimeError(f"Refusing replacement or non-modification: {change}")
        allowed = RESOURCE_PROPERTIES[change["LogicalResourceId"]]
        names = {
            entry.get("Target", {}).get("Name")
            for entry in change.get("Details", [])
            if entry.get("Target", {}).get("Attribute") == "Properties"
        }
        if names and names != {allowed}:
            raise RuntimeError(f"Refusing unexpected {change['LogicalResourceId']} fields: {names}")

    print(f"Executing scoped change set {change_set_id}")
    cfn.execute_change_set(ChangeSetName=change_set_id, StackName=STACK)
    cfn.get_waiter("stack_update_complete").wait(StackName=STACK)
    live = _template(cfn.get_template(StackName=STACK, TemplateStage="Processed")["TemplateBody"])
    for resource, field in RESOURCE_PROPERTIES.items():
        if live["Resources"][resource]["Properties"][field] != desired["Resources"][resource]["Properties"][field]:
            raise RuntimeError(f"Post-deploy verification failed for {resource}.{field}")
    print(f"Scoped monthly release complete from main {args.sha}")


if __name__ == "__main__":
    main()
