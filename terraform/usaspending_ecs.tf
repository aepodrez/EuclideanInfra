###############################################################################
# USASpending quarterly refresh — ECS Fargate task
#
# USASpending.py has two phases:
#   Phase 1 (UEI resolution): 1 EDGAR + 1 usaspending call per ticker,
#     ~900 tickers × 1.4s ≈ 21 min after the SIC pre-filter.
#   Phase 2 (spending download): 3 API calls per resolved ticker,
#     ~600 tickers × 0.75s ≈ 7 min incremental.
#
# Total runtime: 30–90 min per quarterly run — well over Lambda's 900s cap.
# ECS Fargate removes the time limit. Runs quarterly on Oct 1 (start of new
# US fiscal year) so the prior full FY's data is captured within days of close.
#
# Uses the same euclidean-market-data ECR image and lambda_entry dispatcher as
# the Lambda jobs, via ecs_main.py (same container, null context → no deadline).
###############################################################################

locals {
  usaspending_task_family    = "${var.project_name}-usaspending${local.env_suffix}"
  usaspending_container_name = "usaspending"
  usaspending_image_uri      = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/euclidean-market-data:latest"
}

# ---------------------------------------------------------------------------
# CloudWatch log group
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "usaspending" {
  name              = "/ecs/${local.usaspending_task_family}"
  retention_in_days = 90
  tags              = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM — execution role (ECR pull + CloudWatch Logs)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "usaspending_execution" {
  name = "${var.project_name}-usaspending-exec${local.env_suffix}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "usaspending_execution" {
  role       = aws_iam_role.usaspending_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------------
# IAM — task role (S3 read/write for pyData/Intermediate)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "usaspending_task" {
  name = "${var.project_name}-usaspending-task${local.env_suffix}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "usaspending_task_s3" {
  name = "${var.project_name}-usaspending-task-s3${local.env_suffix}"
  role = aws_iam_role.usaspending_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWritePyData"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/pyData/Intermediate/*",
        ]
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.pipeline_data.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["pyData/Intermediate/*"] }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# ECS task definition
# 1 vCPU / 2 GB — USASpending.py is I/O-bound (external API calls);
# no heavy computation or in-memory data loading.
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "usaspending" {
  family                   = local.usaspending_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.usaspending_execution.arn
  task_role_arn            = aws_iam_role.usaspending_task.arn

  container_definitions = jsonencode([
    {
      name       = local.usaspending_container_name
      image      = local.usaspending_image_uri
      essential  = true
      entryPoint = ["python3"]
      command    = ["/var/task/ecs_main.py"]
      environment = [
        { name = "JOB",           value = "usaspending" },
        { name = "S3_BUCKET",     value = aws_s3_bucket.pipeline_data.bucket },
        { name = "PYDATA_PREFIX", value = "pyData/Intermediate" },
        { name = "UNIVERSE_KEY",  value = "universe/universe.csv" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.usaspending.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — quarterly schedule: Oct 1 at 06:00 UTC
# Oct 1 = start of new US federal FY, so FY2025 data is fully settled.
# Also fires Jan 1, Apr 1, Jul 1 for incremental top-ups.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "usaspending" {
  name                = "${var.project_name}-usaspending-quarterly${local.env_suffix}"
  description         = "Quarterly USASpending refresh (Oct 1 = new FY; Jan/Apr/Jul = top-up)"
  schedule_expression = "cron(0 6 1 1,4,7,10 ? *)"
  tags                = local.common_tags
}

resource "aws_iam_role" "usaspending_events" {
  name = "${var.project_name}-usaspending-events${local.env_suffix}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "usaspending_events" {
  name = "${var.project_name}-usaspending-events${local.env_suffix}"
  role = aws_iam_role.usaspending_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = ["${aws_ecs_task_definition.usaspending.arn_without_revision}:*"]
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.usaspending_execution.arn,
          aws_iam_role.usaspending_task.arn,
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "usaspending" {
  rule     = aws_cloudwatch_event_rule.usaspending.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.usaspending_events.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.usaspending.arn
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = [for s in aws_subnet.public : s.id]
      security_groups  = [aws_security_group.ecs.id]
      assign_public_ip = true
    }
  }
}
