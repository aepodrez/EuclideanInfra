###############################################################################
# Annual Fama-French portfolio rebalance — ECS Fargate task
#
# Runs July 1 at 06:00 UTC each year. No Lambda time limit; always re-downloads
# all ~5,700 companyfacts JSON from SEC fresh (FF_FORCE_COMPANYFACTS_REFRESH=1)
# so book equity uses the latest 10-K filings for each new formation year.
#
# The task appends a new formation-date vintage to Static/ff3_portfolios.csv so
# fama-french-daily/monthly can do a point-in-time portfolio lookup for backtests.
###############################################################################

locals {
  ff_task_family    = "${var.project_name}-build-ff-portfolios${local.env_suffix}"
  ff_container_name = "build-ff-portfolios"
  # Same ECR repo as the market-data Lambda; CI updates the task definition image on every deploy.
  ff_image_uri = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${data.aws_region.current.name}.amazonaws.com/euclidean-market-data:latest"
}

# ---------------------------------------------------------------------------
# CloudWatch log group
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "build_ff_portfolios" {
  name              = "/ecs/${local.ff_task_family}"
  retention_in_days = 90
  tags              = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM — execution role (ECR pull + CloudWatch Logs)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "build_ff_portfolios_execution" {
  name = "${var.project_name}-build-ff-exec${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "build_ff_portfolios_execution" {
  role       = aws_iam_role.build_ff_portfolios_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------------
# IAM — task role (S3 access for universe, pyData, Static)
# ---------------------------------------------------------------------------
resource "aws_iam_role" "build_ff_portfolios_task" {
  name = "${var.project_name}-build-ff-task${local.env_suffix}"

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

resource "aws_iam_role_policy" "build_ff_portfolios_task_s3" {
  name = "${var.project_name}-build-ff-task-s3${local.env_suffix}"
  role = aws_iam_role.build_ff_portfolios_task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "S3ReadUniverse"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.pipeline_data.arn}/universe/universe.csv"]
      },
      {
        Sid    = "S3ReadWritePyDataAndStatic"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/pyData/Intermediate/*",
          "${aws_s3_bucket.pipeline_data.arn}/pyData/EDGAR/*",
          "${aws_s3_bucket.pipeline_data.arn}/Static/*",
        ]
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.pipeline_data.arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["pyData/Intermediate/*", "pyData/EDGAR/*", "Static/*", "universe/*"]
          }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# ECS task definition
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "build_ff_portfolios" {
  family                   = local.ff_task_family
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "2048"
  memory                   = "8192"
  execution_role_arn       = aws_iam_role.build_ff_portfolios_execution.arn
  task_role_arn            = aws_iam_role.build_ff_portfolios_task.arn

  container_definitions = jsonencode([
    {
      name       = local.ff_container_name
      image      = local.ff_image_uri
      essential  = true
      entryPoint = ["python3"]
      command    = ["/var/task/ecs_main.py"]
      environment = [
        { name = "JOB", value = "build_ff_portfolios" },
        { name = "S3_BUCKET", value = aws_s3_bucket.pipeline_data.bucket },
        { name = "PYDATA_PREFIX", value = "pyData/Intermediate" },
        { name = "UNIVERSE_KEY", value = "universe/universe.csv" },
        { name = "FF_FORCE_COMPANYFACTS_REFRESH", value = "1" },
        { name = "SEC_EMAIL", value = "podreze03@gmail.com" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.build_ff_portfolios.name
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "ecs"
        }
      }
    }
  ])

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — annual schedule: July 1 at 00:00 ET (04:00 UTC, EDT)
# Runs at midnight ET so the 1-2h EDGAR companyfacts download finishes well before
# fama-french-daily at 06:15 UTC needs the updated portfolio file.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "build_ff_portfolios" {
  name                = "${var.project_name}-build-ff-portfolios-annual${local.env_suffix}"
  description         = "Annual Fama-French portfolio rebalance (July 1 formation date)"
  schedule_expression = "cron(0 4 1 7 ? *)"
  tags                = local.common_tags
}

resource "aws_iam_role" "build_ff_portfolios_events" {
  name = "${var.project_name}-build-ff-events${local.env_suffix}"

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

resource "aws_iam_role_policy" "build_ff_portfolios_events" {
  name = "${var.project_name}-build-ff-events${local.env_suffix}"
  role = aws_iam_role.build_ff_portfolios_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = ["${aws_ecs_task_definition.build_ff_portfolios.arn_without_revision}:*"]
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.build_ff_portfolios_execution.arn,
          aws_iam_role.build_ff_portfolios_task.arn,
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "build_ff_portfolios" {
  rule     = aws_cloudwatch_event_rule.build_ff_portfolios.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.build_ff_portfolios_events.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.build_ff_portfolios.arn
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = [for s in aws_subnet.public : s.id]
      security_groups  = [aws_security_group.ecs.id]
      assign_public_ip = true
    }
  }
}
