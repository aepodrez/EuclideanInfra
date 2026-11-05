import sys
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template


INFRA = Path(__file__).parents[1] / "infra"
sys.path.insert(0, str(INFRA))

from stacks.euclidean_infra_stack import EuclideanInfraStack  # noqa: E402


def test_active_ecs_tasks_keep_reviewed_resource_sizes():
    app = cdk.App()
    stack = EuclideanInfraStack(
        app, "Test", env=cdk.Environment(account="954976294836", region="us-east-1")
    )
    resources = Template.from_stack(stack).to_json()["Resources"]
    tasks = {
        resource["Properties"]["Family"]: resource["Properties"]
        for resource in resources.values()
        if resource["Type"] == "AWS::ECS::TaskDefinition"
    }

    assert tasks["euclidean-build-ff-portfolios"]["Cpu"] == "1024"
    assert tasks["euclidean-build-ff-portfolios"]["Memory"] == "8192"
    assert tasks["euclidean-usaspending"]["Cpu"] == "1024"
    assert tasks["euclidean-usaspending"]["Memory"] == "2048"
