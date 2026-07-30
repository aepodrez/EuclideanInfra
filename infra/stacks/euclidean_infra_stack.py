"""EuclideanInfra stack — the shared foundation: S3 data bucket, VPC/subnets,
ECS cluster, SNS notifications topic, the monthly decision Step Function,
CloudWatch-Logs→Firehose→S3 archival, and the 8 SSM parameters that form the
cross-repo contract.

Faithful CDK translation of `EuclideanInfra/terraform/*.tf`.

Import strategy (highest-stakes repo — three buckets):

1. **Imported** via `cdk import`: bucket, SSM params, SNS topic, ECS cluster,
   state machine, Firehose stream, 11 IAM roles + 1 managed policy, 3 log
   groups, 5 EventBridge rules, and the EC2 estate (VPC, 4 subnets, 2 route
   tables, security group, IGW, S3 gateway endpoint). EC2 resources import by
   physical id (from the cached TF state, via the generator's --extra file)
   and their properties here are transcribed from live state — a mismatch on
   e.g. CidrBlock would mean *replacement* at converge, so the post-import
   `cdk diff` must be scrutinized before deploying.

2. **Created by the converge deploy** (guarded by `import_mode`; all are
   overwrite-safe upserts or revision-registrations, never destructive):
   bucket policy, topic policy, CloudWatch Logs account policy, the two
   github-cicd user policies (legacy AWS::IAM::Policy → PutUserPolicy upsert),
   both ECS task definitions (new revision under the same family — PCM
   precedent), and every rule's Targets.

3. **Left unmanaged in AWS** (keep working, just not in CFN): the confirmed
   SNS email subscription + dormant SMS one (CFN cannot import them — plan
   decision), the IGW→VPC attachment, the routes + route-table associations +
   their S3-endpoint association (adopting the tables does not adopt their
   routes; recreating a live route errors, so they stay unmanaged), the 5 S3
   folder-marker objects, and the operator user's managed-policy attachment.

The Step Function definition is the live one verbatim (assets/
pipeline_definition.json, fetched from AWS and equal to what the TF produced)
— translate it to CDK-native constructs in a later pass if ever needed.

ECS event targets use bare-family task-definition ARNs (no :revision) so
EventBridge always launches the latest ACTIVE revision — the pattern the
ibes_eps target already proved; this also stops target churn when the
converge/CI registers new revisions.
"""
import json
import os

from aws_cdk import (
    CfnResource,
    RemovalPolicy,
    Stack,
    Tags,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_events as events,
    aws_iam as iam,
    aws_kinesisfirehose as firehose,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_ssm as ssm,
    aws_stepfunctions as sfn,
)
from constructs import Construct

ACCOUNT = "954976294836"
REGION = "us-east-1"

# ---- physical identities (from live state; import adopts, never recreates) ----
BUCKET_NAME = "euclidean-pipeline-954976294836"
BUCKET_ARN = f"arn:aws:s3:::{BUCKET_NAME}"
VPC_ID = "vpc-01adb745340a1f5db"
VPC_CIDR = "10.0.0.0/16"
PUBLIC_SUBNETS = [  # (id, cidr, az, name-tag)
    ("subnet-0cb23cb16a893e69b", "10.0.101.0/24", "us-east-1a", "euclidean-public-subnet-a"),
    ("subnet-0c7d5564c847ec3b9", "10.0.102.0/24", "us-east-1b", "euclidean-public-subnet-b"),
]
PRIVATE_SUBNETS = [
    ("subnet-05a989346b1fdf225", "10.0.1.0/24", "us-east-1a", "euclidean-private-subnet-a"),
    ("subnet-07432d8f8781ee2e7", "10.0.2.0/24", "us-east-1b", "euclidean-private-subnet-b"),
]
IGW_ID = "igw-0132309dae0ef36ee"
PUBLIC_RT_ID = "rtb-0a3c1d3ff0041e7e9"
PRIVATE_RT_ID = "rtb-0a57945e192594b31"
SG_ID = "sg-010ca983e1744a0f9"
S3_ENDPOINT_ID = "vpce-05a9d647eb131eae6"

CLUSTER_NAME = "euclidean-cluster"
CLUSTER_ARN = f"arn:aws:ecs:{REGION}:{ACCOUNT}:cluster/{CLUSTER_NAME}"
TOPIC_NAME = "euclidean-pipeline-notifications"
TOPIC_ARN = f"arn:aws:sns:{REGION}:{ACCOUNT}:{TOPIC_NAME}"
SFN_NAME = "euclidean-pipeline"
SFN_ARN = f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{SFN_NAME}"
FIREHOSE_NAME = "euclidean-cwlogs-to-s3"
FIREHOSE_ARN = f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/{FIREHOSE_NAME}"

USASPENDING_FAMILY = "euclidean-usaspending"
FF_FAMILY = "euclidean-build-ff-portfolios"
REFINITIV_FAMILY = "euclidean-data-ingress-refinitiv"  # task def registered by DataIngressModel
MARKET_DATA_IMAGE = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/euclidean-market-data:latest"

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")


def role_arn(name: str) -> str:
    return f"arn:aws:iam::{ACCOUNT}:role/{name}"


class EuclideanInfraStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        import_mode = self.node.try_get_context("import_mode")

        def name_tags(name: str):
            # Import changesets reject the Tags property; Name tags only at converge.
            return None if import_mode else [{"key": "Name", "value": name}]

        # =====================================================================
        # EC2 estate (properties transcribed from live state — see docstring)
        # =====================================================================
        ec2.CfnVPC(
            self, "Vpc",
            cidr_block=VPC_CIDR,
            enable_dns_hostnames=True,
            enable_dns_support=True,
            tags=name_tags("euclidean-vpc"),
        )
        ec2.CfnInternetGateway(self, "Igw", tags=name_tags("euclidean-igw"))
        for label, subnets, public in (("Public", PUBLIC_SUBNETS, True), ("Private", PRIVATE_SUBNETS, False)):
            for (sid, cidr, az, name) in subnets:
                ec2.CfnSubnet(
                    self, f"{label}Subnet{az[-1].upper()}",
                    vpc_id=VPC_ID,
                    cidr_block=cidr,
                    availability_zone=az,
                    map_public_ip_on_launch=public,
                    tags=name_tags(name),
                )
        ec2.CfnRouteTable(self, "PublicRouteTable", vpc_id=VPC_ID, tags=name_tags("euclidean-public-rt"))
        ec2.CfnRouteTable(self, "PrivateRouteTable", vpc_id=VPC_ID, tags=name_tags("euclidean-private-rt"))
        ec2.CfnSecurityGroup(
            self, "EcsSecurityGroup",
            group_name="euclidean-ecs-sg",
            group_description="Security group for ECS tasks",
            vpc_id=VPC_ID,
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="-1", cidr_ip="0.0.0.0/0", description="Allow all outbound traffic",
                )
            ],
            tags=name_tags("euclidean-ecs-sg"),
        )
        ec2.CfnVPCEndpoint(
            self, "S3Endpoint",
            vpc_id=VPC_ID,
            service_name=f"com.amazonaws.{REGION}.s3",
            vpc_endpoint_type="Gateway",
            route_table_ids=[PUBLIC_RT_ID, PRIVATE_RT_ID],
        )

        # =====================================================================
        # S3 data bucket (versioning / SSE / public-access-block fold into it)
        # =====================================================================
        bucket = s3.CfnBucket(
            self, "PipelineDataBucket",
            bucket_name=BUCKET_NAME,
            versioning_configuration=s3.CfnBucket.VersioningConfigurationProperty(status="Enabled"),
            lifecycle_configuration=s3.CfnBucket.LifecycleConfigurationProperty(
                rules=[
                    s3.CfnBucket.RuleProperty(
                        id="retain-noncurrent-versions-for-30-days",
                        status="Enabled",
                        prefix="",
                        noncurrent_version_expiration=s3.CfnBucket.NoncurrentVersionExpirationProperty(
                            noncurrent_days=30,
                        ),
                        abort_incomplete_multipart_upload=s3.CfnBucket.AbortIncompleteMultipartUploadProperty(
                            days_after_initiation=7,
                        ),
                    ),
                    s3.CfnBucket.RuleProperty(
                        id="remove-expired-delete-markers",
                        status="Enabled",
                        prefix="",
                        expired_object_delete_marker=True,
                    ),
                ]
            ),
            bucket_encryption=s3.CfnBucket.BucketEncryptionProperty(
                server_side_encryption_configuration=[
                    s3.CfnBucket.ServerSideEncryptionRuleProperty(
                        server_side_encryption_by_default=s3.CfnBucket.ServerSideEncryptionByDefaultProperty(
                            sse_algorithm="AES256"
                        )
                    )
                ]
            ),
            public_access_block_configuration=s3.CfnBucket.PublicAccessBlockConfigurationProperty(
                block_public_acls=True,
                block_public_policy=True,
                ignore_public_acls=True,
                restrict_public_buckets=True,
            ),
        )
        bucket.apply_removal_policy(RemovalPolicy.RETAIN)

        # =====================================================================
        # SNS notifications topic (subscriptions stay unmanaged — see docstring)
        # =====================================================================
        sns.CfnTopic(self, "NotificationsTopic", topic_name=TOPIC_NAME)

        # =====================================================================
        # ECS cluster
        # =====================================================================
        ecs.CfnCluster(
            self, "Cluster",
            cluster_name=CLUSTER_NAME,
            cluster_settings=[ecs.CfnCluster.ClusterSettingsProperty(name="containerInsights", value="enabled")],
            capacity_providers=["FARGATE", "FARGATE_SPOT"],
            default_capacity_provider_strategy=[
                ecs.CfnCluster.CapacityProviderStrategyItemProperty(
                    capacity_provider="FARGATE", weight=1, base=1
                )
            ],
        )

        # =====================================================================
        # IAM roles (names + inline-policy names match Terraform exactly)
        # =====================================================================
        iam.Role(
            self, "StepFunctionRole",
            role_name="euclidean-sfn-role",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
            inline_policies={
                "euclidean-sfn-policy": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["lambda:InvokeFunction"],
                        resources=[
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-universe",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-alpha-model",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-execution-model",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-pred-*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["ecs:RunTask"],
                        resources=[
                            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/euclidean-universe:*",
                            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/euclidean-data-ingress-downloads:*",
                            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/euclidean-data-ingress-refinitiv:*",
                            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/euclidean-data-ingress-predictors:*",
                            f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/euclidean-portfolio-construction:*",
                        ],
                    ),
                    iam.PolicyStatement(
                        actions=["ecs:StopTask", "ecs:DescribeTasks"],
                        resources=["*"],
                        conditions={"StringEquals": {"ecs:cluster": CLUSTER_ARN}},
                    ),
                    iam.PolicyStatement(
                        actions=["iam:PassRole"],
                        resources=["*"],
                        conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                    ),
                    iam.PolicyStatement(
                        actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                        resources=[f"arn:aws:events:{REGION}:{ACCOUNT}:rule/StepFunctionsGetEventsForECSTaskRule"],
                    ),
                    iam.PolicyStatement(
                        actions=[
                            "logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
                            "logs:DeleteLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                            "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
                        ],
                        resources=["*"],
                    ),
                ])
            },
        )

        iam.Role(
            self, "PipelineSchedulerRole",
            role_name="euclidean-pipeline-scheduler",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            inline_policies={
                "euclidean-pipeline-scheduler": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(actions=["states:StartExecution"], resources=[SFN_ARN]),
                ])
            },
        )

        # -- usaspending ECS task roles --
        iam.Role(
            self, "UsaspendingExecutionRole",
            role_name="euclidean-usaspending-exec",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy")],
        )
        iam.Role(
            self, "UsaspendingTaskRole",
            role_name="euclidean-usaspending-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={
                "euclidean-usaspending-task-s3": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        sid="S3ReadWritePyData",
                        actions=["s3:GetObject", "s3:PutObject"],
                        resources=[f"{BUCKET_ARN}/pyData/Intermediate/*"],
                    ),
                    iam.PolicyStatement(
                        sid="S3List",
                        actions=["s3:ListBucket"],
                        resources=[BUCKET_ARN],
                        conditions={"StringLike": {"s3:prefix": ["pyData/Intermediate/*"]}},
                    ),
                ])
            },
        )
        iam.Role(
            self, "UsaspendingEventsRole",
            role_name="euclidean-usaspending-events",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            inline_policies={
                "euclidean-usaspending-events": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["ecs:RunTask"],
                        resources=[f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{USASPENDING_FAMILY}:*"],
                    ),
                    iam.PolicyStatement(
                        actions=["iam:PassRole"],
                        resources=[role_arn("euclidean-usaspending-exec"), role_arn("euclidean-usaspending-task")],
                        conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                    ),
                ])
            },
        )

        # -- build-ff-portfolios ECS task roles --
        iam.Role(
            self, "FfExecutionRole",
            role_name="euclidean-build-ff-exec",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy")],
        )
        iam.Role(
            self, "FfTaskRole",
            role_name="euclidean-build-ff-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={
                "euclidean-build-ff-task-s3": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        sid="S3ReadUniverse",
                        actions=["s3:GetObject"],
                        resources=[f"{BUCKET_ARN}/universe/universe.csv"],
                    ),
                    iam.PolicyStatement(
                        sid="S3ReadWritePyDataAndStatic",
                        actions=["s3:GetObject", "s3:PutObject"],
                        resources=[
                            f"{BUCKET_ARN}/pyData/Intermediate/*",
                            f"{BUCKET_ARN}/pyData/EDGAR/*",
                            f"{BUCKET_ARN}/Static/*",
                        ],
                    ),
                    iam.PolicyStatement(
                        sid="S3List",
                        actions=["s3:ListBucket"],
                        resources=[BUCKET_ARN],
                        conditions={"StringLike": {"s3:prefix": [
                            "pyData/Intermediate/*", "pyData/EDGAR/*", "Static/*", "universe/*"]}},
                    ),
                ])
            },
        )
        iam.Role(
            self, "FfEventsRole",
            role_name="euclidean-build-ff-events",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            inline_policies={
                "euclidean-build-ff-events": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["ecs:RunTask"],
                        resources=[f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{FF_FAMILY}:*"],
                    ),
                    iam.PolicyStatement(
                        actions=["iam:PassRole"],
                        resources=[role_arn("euclidean-build-ff-exec"), role_arn("euclidean-build-ff-task")],
                        conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                    ),
                ])
            },
        )

        # -- ibes-eps events role (task def + task roles live in DataIngressModel) --
        iam.Role(
            self, "IbesEpsEventsRole",
            role_name="euclidean-ibes-eps-events",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
            inline_policies={
                "euclidean-ibes-eps-events": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["ecs:RunTask"],
                        resources=[f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{REFINITIV_FAMILY}:*"],
                    ),
                    iam.PolicyStatement(
                        actions=["iam:PassRole"],
                        resources=[
                            role_arn("euclidean-data-ingress-execution-role"),
                            role_arn("euclidean-data-ingress-task-role"),
                        ],
                        conditions={"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                    ),
                ])
            },
        )

        # -- CloudWatch Logs -> Firehose -> S3 archival roles --
        iam.Role(
            self, "FirehoseToS3Role",
            role_name="euclidean-firehose-cwlogs",
            assumed_by=iam.ServicePrincipal("firehose.amazonaws.com"),
            inline_policies={
                "euclidean-firehose-cwlogs": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=[
                            "s3:AbortMultipartUpload", "s3:GetBucketLocation", "s3:GetObject",
                            "s3:ListBucket", "s3:ListBucketMultipartUploads", "s3:PutObject",
                        ],
                        resources=[BUCKET_ARN, f"{BUCKET_ARN}/*"],
                    ),
                ])
            },
        )
        iam.Role(
            self, "CwLogsToFirehoseRole",
            role_name="euclidean-cwlogs-to-firehose",
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("logs.amazonaws.com"),
                iam.ServicePrincipal(f"logs.{REGION}.amazonaws.com"),
            ),
            inline_policies={
                "euclidean-cwlogs-to-firehose": iam.PolicyDocument(statements=[
                    iam.PolicyStatement(
                        actions=["firehose:DescribeDeliveryStream", "firehose:PutRecord", "firehose:PutRecordBatch"],
                        resources=[FIREHOSE_ARN, f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/*"],
                    ),
                ])
            },
        )

        # -- operator read access to SFN executions (attachment to the user
        #    stays unmanaged in AWS; CFN has no importable user-attachment) --
        iam.ManagedPolicy(
            self, "OperatorSfnRead",
            managed_policy_name="euclidean-operator-sfn-read",
            description="Allow operator to list and inspect Step Functions executions for log export windows",
            statements=[
                iam.PolicyStatement(
                    actions=["states:ListExecutions", "states:DescribeExecution"],
                    resources=[SFN_ARN, f"arn:aws:states:{REGION}:{ACCOUNT}:execution:{SFN_NAME}:*"],
                ),
            ],
        )

        # =====================================================================
        # Log groups
        # =====================================================================
        sfn_log_group = logs.LogGroup(
            self, "StepFunctionLogGroup",
            log_group_name=f"/aws/stepfunctions/{SFN_NAME}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.RETAIN,
        )
        usaspending_log_group = logs.LogGroup(
            self, "UsaspendingLogGroup",
            log_group_name=f"/ecs/{USASPENDING_FAMILY}",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.RETAIN,
        )
        ff_log_group = logs.LogGroup(
            self, "FfLogGroup",
            log_group_name=f"/ecs/{FF_FAMILY}",
            retention=logs.RetentionDays.THREE_MONTHS,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # =====================================================================
        # Step Function — monthly decision pipeline (live definition verbatim)
        # =====================================================================
        with open(os.path.join(ASSETS_DIR, "pipeline_definition.json")) as f:
            pipeline_definition = f.read()
        sfn.CfnStateMachine(
            self, "Pipeline",
            state_machine_name=SFN_NAME,
            role_arn=role_arn("euclidean-sfn-role"),
            definition_string=pipeline_definition,
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                            log_group_arn=f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/stepfunctions/{SFN_NAME}:*"
                        )
                    )
                ],
                include_execution_data=True,
                level="ALL",
            ),
        )

        # =====================================================================
        # Firehose delivery stream (CloudWatch Logs archive → S3)
        # =====================================================================
        firehose.CfnDeliveryStream(
            self, "CwLogsFirehose",
            delivery_stream_name=FIREHOSE_NAME,
            delivery_stream_type="DirectPut",
            extended_s3_destination_configuration=firehose.CfnDeliveryStream.ExtendedS3DestinationConfigurationProperty(
                role_arn=role_arn("euclidean-firehose-cwlogs"),
                bucket_arn=BUCKET_ARN,
                buffering_hints=firehose.CfnDeliveryStream.BufferingHintsProperty(
                    size_in_m_bs=5, interval_in_seconds=300,
                ),
                compression_format="GZIP",
                prefix="cloudwatch-logs/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
                error_output_prefix="cloudwatch-logs/failed/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/",
            ),
        )

        # =====================================================================
        # SSM parameters — the cross-repo contract (values are the live ids)
        # =====================================================================
        ssm_values = {
            "S3BucketNameParam": ("/euclidean/s3_bucket_name", BUCKET_NAME),
            "S3BucketArnParam": ("/euclidean/s3_bucket_arn", BUCKET_ARN),
            "VpcIdParam": ("/euclidean/vpc_id", VPC_ID),
            "PrivateSubnetIdsParam": ("/euclidean/private_subnet_ids", ",".join(s[0] for s in PRIVATE_SUBNETS)),
            "PublicSubnetIdsParam": ("/euclidean/public_subnet_ids", ",".join(s[0] for s in PUBLIC_SUBNETS)),
            "EcsClusterArnParam": ("/euclidean/ecs_cluster_arn", CLUSTER_ARN),
            "EcsSecurityGroupIdParam": ("/euclidean/ecs_security_group_id", SG_ID),
            "SnsNotificationsArnParam": ("/euclidean/sns_notifications_arn", TOPIC_ARN),
        }
        for cid, (pname, value) in ssm_values.items():
            ssm.StringParameter(self, cid, parameter_name=pname, string_value=value)

        # =====================================================================
        # EventBridge rules (Targets only at converge — import rejects them
        # alongside the rule, and Lambda/ECS wiring is converge business)
        # =====================================================================
        ecs_target_common = dict(
            task_count=1,
            launch_type="FARGATE",
            network_configuration=events.CfnRule.NetworkConfigurationProperty(
                aws_vpc_configuration=events.CfnRule.AwsVpcConfigurationProperty(
                    subnets=[s[0] for s in PUBLIC_SUBNETS],
                    security_groups=[SG_ID],
                    assign_public_ip="ENABLED",
                )
            ),
        )

        events.CfnRule(
            self, "UsaspendingRule",
            name="euclidean-usaspending-quarterly",
            description="Quarterly USASpending refresh (Oct 1 = new FY; Jan/Apr/Jul = top-up)",
            schedule_expression="cron(0 6 1 1,4,7,10 ? *)",
            targets=None if import_mode else [
                events.CfnRule.TargetProperty(
                    id="Target0",
                    arn=CLUSTER_ARN,
                    role_arn=role_arn("euclidean-usaspending-events"),
                    ecs_parameters=events.CfnRule.EcsParametersProperty(
                        task_definition_arn=f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{USASPENDING_FAMILY}",
                        **ecs_target_common,
                    ),
                )
            ],
        )

        events.CfnRule(
            self, "BuildFfPortfoliosRule",
            name="euclidean-build-ff-portfolios-annual",
            description="Annual Fama-French portfolio rebalance (July 1 formation date)",
            schedule_expression="cron(0 4 1 7 ? *)",
            targets=None if import_mode else [
                events.CfnRule.TargetProperty(
                    id="Target0",
                    arn=CLUSTER_ARN,
                    role_arn=role_arn("euclidean-build-ff-events"),
                    ecs_parameters=events.CfnRule.EcsParametersProperty(
                        task_definition_arn=f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{FF_FAMILY}",
                        **ecs_target_common,
                    ),
                )
            ],
        )

        events.CfnRule(
            self, "IbesEpsRule",
            name="euclidean-ibes-eps-monthly",
            description="Monthly IBES EPS-estimate refresh (Adjusted + Unadjusted) on ECS Fargate",
            schedule_expression="cron(0 5 1 * ? *)",
            targets=None if import_mode else [
                events.CfnRule.TargetProperty(
                    id="Target0",
                    arn=CLUSTER_ARN,
                    role_arn=role_arn("euclidean-ibes-eps-events"),
                    ecs_parameters=events.CfnRule.EcsParametersProperty(
                        # Bare-family ARN: EventBridge resolves the latest ACTIVE
                        # revision registered by the DataIngressModel CI.
                        task_definition_arn=f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{REFINITIV_FAMILY}",
                        **ecs_target_common,
                    ),
                )
            ],
        )

        events.CfnRule(
            self, "PipelineScheduleRule",
            name="euclidean-pipeline-schedule",
            description="Monthly trigger for the Euclidean decision pipeline (1st, 08:30 UTC)",
            schedule_expression="cron(30 8 1 * ? *)",
            targets=None if import_mode else [
                events.CfnRule.TargetProperty(
                    id="Target0",
                    arn=SFN_ARN,
                    role_arn=role_arn("euclidean-pipeline-scheduler"),
                )
            ],
        )

        events.CfnRule(
            self, "PipelineStateChangeRule",
            name="euclidean-pipeline-state-change",
            description="Fires on Step Functions pipeline SUCCEEDED, FAILED, TIMED_OUT, or ABORTED",
            event_pattern={
                "source": ["aws.states"],
                "detail-type": ["Step Functions Execution Status Change"],
                "detail": {
                    "stateMachineArn": [SFN_ARN],
                    "status": ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"],
                },
            },
            targets=None if import_mode else [
                events.CfnRule.TargetProperty(
                    id="Target0",
                    arn=TOPIC_ARN,
                    input_transformer=events.CfnRule.InputTransformerProperty(
                        input_paths_map={"status": "$.detail.status", "name": "$.detail.name"},
                        input_template='"Euclidean pipeline <name>: <status>"',
                    ),
                )
            ],
        )

        # =====================================================================
        # Converge-only resources (all upserts / new-revision registrations)
        # =====================================================================
        if not import_mode:
            s3.CfnBucketPolicy(
                self, "PipelineDataBucketPolicy",
                bucket=BUCKET_NAME,
                policy_document={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowCloudWatchLogsExportGetBucketAcl",
                            "Effect": "Allow",
                            "Principal": {"Service": f"logs.{REGION}.amazonaws.com"},
                            "Action": "s3:GetBucketAcl",
                            "Resource": BUCKET_ARN,
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": ACCOUNT},
                                "ArnLike": {"aws:SourceArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:*"},
                            },
                        },
                        {
                            "Sid": "AllowCloudWatchLogsExportPutObject",
                            "Effect": "Allow",
                            "Principal": {"Service": f"logs.{REGION}.amazonaws.com"},
                            "Action": "s3:PutObject",
                            "Resource": [f"{BUCKET_ARN}/manual-exports/*", f"{BUCKET_ARN}/cloudwatch-logs/*"],
                            "Condition": {
                                "StringEquals": {
                                    "aws:SourceAccount": ACCOUNT,
                                    "s3:x-amz-acl": "bucket-owner-full-control",
                                },
                                "ArnLike": {"aws:SourceArn": f"arn:aws:logs:{REGION}:{ACCOUNT}:*"},
                            },
                        },
                    ],
                },
            )

            sns.CfnTopicPolicy(
                self, "NotificationsTopicPolicy",
                topics=[TOPIC_ARN],
                policy_document={
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Sid": "AllowEventBridgePublish",
                        "Effect": "Allow",
                        "Principal": {"Service": "events.amazonaws.com"},
                        "Action": "sns:Publish",
                        "Resource": TOPIC_ARN,
                    }],
                },
            )

            logs.CfnAccountPolicy(
                self, "AllLogsToFirehose",
                policy_name="euclidean-cwlogs-to-s3",
                policy_type="SUBSCRIPTION_FILTER_POLICY",
                scope="ALL",
                policy_document=json.dumps({
                    "DestinationArn": FIREHOSE_ARN,
                    "RoleArn": role_arn("euclidean-cwlogs-to-firehose"),
                    "FilterPattern": "",
                    "Distribution": "Random",
                }),
            )

            # legacy AWS::IAM::Policy → PutUserPolicy upsert on the CI user
            iam.CfnPolicy(
                self, "GithubCicdLambdaDeploy",
                policy_name="euclidean-github-cicd-lambda-deploy",
                users=["github-cicd"],
                policy_document={
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": ["lambda:UpdateFunctionCode", "lambda:GetFunction",
                                   "lambda:GetFunctionConfiguration"],
                        "Resource": [
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-alpha-model",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-execution-model",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-md-*",
                            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:euclidean-pred-*",
                        ],
                    }],
                },
            )
            iam.CfnPolicy(
                self, "GithubCicdEcsPassRole",
                policy_name="euclidean-github-cicd-ecs-pass-role",
                users=["github-cicd"],
                policy_document={
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Effect": "Allow",
                        "Action": "iam:PassRole",
                        "Resource": f"arn:aws:iam::{ACCOUNT}:role/euclidean-*",
                        "Condition": {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}},
                    }],
                },
            )

            # Lets every repo's `cdk deploy` step (running as github-cicd) assume
            # the CDK bootstrap roles for synth-time SSM lookups and the actual
            # deploy. Standalone AWS::IAM::ManagedPolicy (not CfnPolicy/inline,
            # unlike the two above) — github-cicd's inline-policy budget is
            # already at the 2048-byte account limit.
            iam.ManagedPolicy(
                self, "GithubCicdCdkAssumeRoles",
                managed_policy_name="euclidean-github-cicd-cdk-assume-roles",
                users=[iam.User.from_user_name(self, "GithubCicdUserRef", "github-cicd")],
                statements=[
                    iam.PolicyStatement(
                        actions=["sts:AssumeRole"],
                        resources=[
                            role_arn(f"cdk-hnb659fds-{r}-{ACCOUNT}-{REGION}")
                            for r in ("deploy-role", "file-publishing-role",
                                      "image-publishing-role", "lookup-role", "cfn-exec-role")
                        ],
                    ),
                ],
            )

            # ---- ECS task definitions (new revision under the same family) ----
            usaspending_task = ecs.FargateTaskDefinition(
                self, "UsaspendingTaskDef",
                family=USASPENDING_FAMILY,
                cpu=1024,
                memory_limit_mib=2048,
                execution_role=iam.Role.from_role_arn(
                    self, "UsaspendingExecRoleRef", role_arn("euclidean-usaspending-exec"), mutable=False),
                task_role=iam.Role.from_role_arn(
                    self, "UsaspendingTaskRoleRef", role_arn("euclidean-usaspending-task"), mutable=False),
            )
            usaspending_task.add_container(
                "usaspending",
                image=ecs.ContainerImage.from_registry(MARKET_DATA_IMAGE),
                essential=True,
                entry_point=["python3"],
                command=["/var/task/ecs_main.py"],
                environment={
                    "JOB": "usaspending",
                    "S3_BUCKET": BUCKET_NAME,
                    "PYDATA_PREFIX": "pyData/Intermediate",
                    "UNIVERSE_KEY": "universe/universe.csv",
                },
                logging=ecs.LogDriver.aws_logs(stream_prefix="ecs", log_group=usaspending_log_group),
            )

            ff_task = ecs.FargateTaskDefinition(
                self, "FfTaskDef",
                family=FF_FAMILY,
                cpu=2048,
                memory_limit_mib=8192,
                execution_role=iam.Role.from_role_arn(
                    self, "FfExecRoleRef", role_arn("euclidean-build-ff-exec"), mutable=False),
                task_role=iam.Role.from_role_arn(
                    self, "FfTaskRoleRef", role_arn("euclidean-build-ff-task"), mutable=False),
            )
            ff_task.add_container(
                "build-ff-portfolios",
                image=ecs.ContainerImage.from_registry(MARKET_DATA_IMAGE),
                essential=True,
                entry_point=["python3"],
                command=["/var/task/ecs_main.py"],
                environment={
                    "JOB": "build_ff_portfolios",
                    "S3_BUCKET": BUCKET_NAME,
                    "PYDATA_PREFIX": "pyData/Intermediate",
                    "UNIVERSE_KEY": "universe/universe.csv",
                    "FF_FORCE_COMPANYFACTS_REFRESH": "1",
                    "SEC_EMAIL": "podreze03@gmail.com",
                },
                logging=ecs.LogDriver.aws_logs(stream_prefix="ecs", log_group=ff_log_group),
            )

            Tags.of(self).add("Project", "euclidean")
            Tags.of(self).add("ManagedBy", "cdk")
            Tags.of(self).add("Component", "infra")

        # Foundation stack: nothing here may ever be deleted by a stack
        # operation — keep the Retain that `cdk import` stamped on every
        # imported resource (and extend it to the converge-created ones).
        for child in self.node.find_all():
            if isinstance(child, CfnResource):
                child.apply_removal_policy(RemovalPolicy.RETAIN)
