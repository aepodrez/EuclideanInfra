# ---------------------------------------------------------------------------
# Scheduled IBES EPS-estimate refresh (ECS Fargate).
#
# The Refinitiv ECS task (task def lives in the DataIngressModel repo) now runs
# only the two EPS-estimate scripts — IBESRecommendations + IBESUnadjustedActuals
# moved to the dedicated IBES Lambda image. The task def bakes in
# CROSSSECTION_REFINITIV_SCRIPTS = the two EPS scripts, so a plain scheduled
# RunTask needs no container override.
#
# Cadence: monthly, 1st @ 05:00 UTC. Refinitiv's estimate summary (Frq=M) only
# moves monthly, and this lands before crsp-monthly (05:30), the IBES-CRSP link
# Lambda (07:00) and the 08:30 decision pipeline. These two scripts hit ~2,300
# batches (4 FPIs x full universe), which exceeds Lambda's 900s cap — hence ECS.
#
# The task definition + its execution/task roles are created in DataIngressModel;
# we reference them by naming convention (same pattern as the Step Function's ECS
# task ARNs in iam.tf / step_function.tf).
# ---------------------------------------------------------------------------

locals {
  refinitiv_task_family        = "${var.project_name}-data-ingress-refinitiv${local.env_suffix}"
  refinitiv_execution_role_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_name}-data-ingress-execution-role${local.env_suffix}"
  refinitiv_task_role_arn      = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.project_name}-data-ingress-task-role${local.env_suffix}"
}

resource "aws_cloudwatch_event_rule" "ibes_eps" {
  name                = "${var.project_name}-ibes-eps-monthly${local.env_suffix}"
  description         = "Monthly IBES EPS-estimate refresh (Adjusted + Unadjusted) on ECS Fargate"
  schedule_expression = "cron(0 5 1 * ? *)" # monthly 1st 05:00 UTC
  tags                = local.common_tags
}

resource "aws_iam_role" "ibes_eps_events" {
  name = "${var.project_name}-ibes-eps-events${local.env_suffix}"

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

resource "aws_iam_role_policy" "ibes_eps_events" {
  name = "${var.project_name}-ibes-eps-events${local.env_suffix}"
  role = aws_iam_role.ibes_eps_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecs:RunTask"]
        Resource = [
          "arn:aws:ecs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:task-definition/${local.refinitiv_task_family}:*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          local.refinitiv_execution_role_arn,
          local.refinitiv_task_role_arn,
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "ibes_eps" {
  rule     = aws_cloudwatch_event_rule.ibes_eps.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.ibes_eps_events.arn

  ecs_target {
    # Family name (no revision) so EventBridge launches the latest ACTIVE revision
    # registered by the DataIngressModel CI. The number of task-def revisions the
    # role may RunTask is scoped by the ":*" ARN in the policy above.
    task_definition_arn = local.refinitiv_task_family
    task_count          = 1
    launch_type         = "FARGATE"

    network_configuration {
      subnets          = [for s in aws_subnet.public : s.id]
      security_groups  = [aws_security_group.ecs.id]
      assign_public_ip = true
    }
  }
}
