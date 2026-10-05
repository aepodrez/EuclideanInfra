"""The month-end release must not carry unrelated infrastructure drift."""

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "infra" / "tools"))
from deploy_scoped_monthly import HIGH_MEMORY_TASK_ARN, scoped_template


def _templates():
    current = {"Resources": {
        "Pipeline": {"Type": "AWS::StepFunctions::StateMachine", "Properties": {
            "DefinitionString": "old", "StateMachineName": "monthly",
        }},
        "StepFunctionRoleC4BAB6F8": {"Type": "AWS::IAM::Role", "Properties": {
            "Policies": [{"PolicyDocument": {"Statement": [{
                "Action": "ecs:RunTask", "Resource": ["existing-task"],
            }]}}],
        }},
        "UnrelatedRetention": {"Type": "AWS::S3::Bucket", "Properties": {
            "LifecycleConfiguration": "deployed",
        }},
    }}
    candidate = copy.deepcopy(current)
    candidate["Resources"]["Pipeline"]["Properties"]["DefinitionString"] = "new"
    candidate["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"][0]["Resource"].append(HIGH_MEMORY_TASK_ARN)
    candidate["Resources"]["UnrelatedRetention"]["Properties"][
        "LifecycleConfiguration"
    ] = "unreleased"
    return current, candidate


def test_scoped_template_preserves_unrelated_live_resources():
    current, candidate = _templates()
    result = scoped_template(current, candidate)
    assert result["Resources"]["UnrelatedRetention"] == current["Resources"][
        "UnrelatedRetention"
    ]
    assert result["Resources"]["Pipeline"]["Properties"]["DefinitionString"] == "new"
    assert current["Resources"]["Pipeline"]["Properties"]["DefinitionString"] == "old"


def test_scoped_template_rejects_extra_iam_permission():
    current, candidate = _templates()
    candidate["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"][0][
        "PolicyDocument"
    ]["Statement"][0]["Resource"].append("unexpected-task")
    with pytest.raises(RuntimeError, match="changes IAM beyond"):
        scoped_template(current, candidate)


def test_scoped_template_accepts_pipeline_only_revision_after_iam_cutover():
    current, candidate = _templates()
    current["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"] = copy.deepcopy(
        candidate["Resources"]["StepFunctionRoleC4BAB6F8"]["Properties"]["Policies"]
    )
    result = scoped_template(current, candidate)
    assert result["Resources"]["Pipeline"]["Properties"]["DefinitionString"] == "new"
    assert result["Resources"]["StepFunctionRoleC4BAB6F8"] == current["Resources"]["StepFunctionRoleC4BAB6F8"]
