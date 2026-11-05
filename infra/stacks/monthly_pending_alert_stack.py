"""Isolated month-end status alert; scoped releases must not deploy foundation drift."""
from __future__ import annotations

import os

from aws_cdk import Duration, Stack, aws_events as events
from aws_cdk import aws_events_targets as event_targets
from aws_cdk import aws_iam as iam, aws_lambda as lambda_
from constructs import Construct

from .euclidean_infra_stack import ACCOUNT, REGION, BUCKET_ARN, BUCKET_NAME, TOPIC_ARN


class MonthlyPendingAlertStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        assets = os.path.join(os.path.dirname(__file__), "..", "..", "lambdas")
        pending_alert = lambda_.Function(
            self, "MonthlyPendingStatusAlert",
            function_name="euclidean-monthly-pending-status-alert",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset(os.path.join(assets, "monthly_pending_alert")),
            timeout=Duration.minutes(3),
            memory_size=256,
            reserved_concurrent_executions=1,
            environment={
                "S3_BUCKET": BUCKET_NAME,
                "STATUS_FUNCTION": "euclidean-predictor-availability",
                "NOTIFICATIONS_TOPIC_ARN": TOPIC_ARN,
            },
        )
        pending_alert.add_to_role_policy(iam.PolicyStatement(
            actions=["lambda:InvokeFunction"],
            resources=[f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-predictor-availability"],
        ))
        pending_alert.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:GetObject"],
            resources=[
                f"{BUCKET_ARN}/portfolio-construction/runs/*/optimal_portfolio_meta.json",
                f"{BUCKET_ARN}/portfolio-construction/runs/*/optimal_portfolio.csv",
            ],
        ))
        pending_alert.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[f"{BUCKET_ARN}/control-plane/monthly-status-alerts/*"],
        ))
        pending_alert.add_to_role_policy(iam.PolicyStatement(
            actions=["sns:Publish"], resources=[TOPIC_ARN],
        ))
        # One UTC invocation maps to 9 a.m. New York; the handler ignores the other.
        events.Rule(
            self, "MonthlyPendingStatusSchedule",
            rule_name="euclidean-monthly-pending-status-9am",
            description="Report a specific month-end blocker at 9 a.m. New York time",
            schedule=events.Schedule.cron(minute="0", hour="13,14", day="1"),
            targets=[event_targets.LambdaFunction(pending_alert)],
        )
