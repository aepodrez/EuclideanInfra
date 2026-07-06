#!/usr/bin/env python3
"""CDK app for the EuclideanInfra repo (replaces the Terraform Cloud workspace)."""
import os

import aws_cdk as cdk

from stacks.euclidean_infra_stack import EuclideanInfraStack

app = cdk.App()

account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-east-1"

EuclideanInfraStack(
    app,
    "EuclideanInfra",
    stack_name="euclidean-infra",
    env=cdk.Environment(account=account, region=region),
)

app.synth()
