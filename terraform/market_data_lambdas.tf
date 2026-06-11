###############################################################################
# Market-data overnight Lambdas (yfinance / FRED / Ritter)
#
# One container image (ECR), one Lambda function per script, dispatched by the
# JOB env var. EventBridge schedules each on an appropriate cadence.
#
# NOTE: the image must be built+pushed before these Lambdas can be created:
#   terraform apply -target=aws_ecr_repository.market_data   # first time only
#   lambdas/market_data/build_and_push.sh
#   terraform apply
###############################################################################

resource "aws_ecr_repository" "market_data" {
  name                 = "euclidean-market-data"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = false
  }

  tags = local.common_tags
}

# Keep only the most recent images
resource "aws_ecr_lifecycle_policy" "market_data" {
  repository = aws_ecr_repository.market_data.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------------------
# Job catalogue: name suffix -> {job dispatch key, schedule}
# ---------------------------------------------------------------------------
locals {
  market_data_jobs = {
    "vix"                 = { job = "vix", schedule = "cron(30 5 * * ? *)" }                       # daily 05:30
    "crsp-daily"          = { job = "crsp_daily", schedule = "cron(30 5 * * ? *)" }                # daily 05:30
    "fama-french-daily"   = { job = "fama_french_daily", schedule = "cron(30 5 * * ? *)" }         # daily 05:30
    "crsp-monthly"        = { job = "crsp_monthly", schedule = "cron(30 5 1 * ? *)" }              # monthly 1st 05:30
    "fama-french-monthly" = { job = "fama_french_monthly", schedule = "cron(30 5 1 * ? *)" }       # monthly 1st 05:30
    "market-returns"      = { job = "market_returns", schedule = "cron(30 5 1 * ? *)" }            # monthly 1st 05:30
    "treasury-bill-3m"    = { job = "treasury_bill_3m", schedule = "cron(30 5 1 1,4,7,10 ? *)" }   # quarterly
    "crsp-distributions"  = { job = "crsp_distributions", schedule = "cron(30 5 1 1,4,7,10 ? *)" } # quarterly
    "ipo-dates"           = { job = "ipo_dates", schedule = "cron(30 5 1 1,4,7,10 ? *)" }          # quarterly
  }

  market_data_image_uri = "${aws_ecr_repository.market_data.repository_url}:latest"
}

# ---------------------------------------------------------------------------
# Shared IAM role for all market-data Lambdas
# ---------------------------------------------------------------------------
resource "aws_iam_role" "market_data" {
  name = "${var.project_name}-market-data${local.env_suffix}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "market_data_basic_logs" {
  role       = aws_iam_role.market_data.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "market_data" {
  name = "${var.project_name}-market-data${local.env_suffix}"
  role = aws_iam_role.market_data.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "S3ReadUniverse"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${aws_s3_bucket.pipeline_data.arn}/universe.csv"]
      },
      {
        Sid    = "S3ReadWritePyDataAndStatic"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/pyData/Intermediate/*",
          "${aws_s3_bucket.pipeline_data.arn}/Static/*",
        ]
      },
      {
        Sid      = "S3List"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.pipeline_data.arn]
        Condition = {
          StringLike = { "s3:prefix" = ["pyData/Intermediate/*", "Static/*"] }
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda functions (one per job, all on the same image)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "market_data" {
  for_each          = local.market_data_jobs
  name              = "/aws/lambda/${var.project_name}-md-${each.key}${local.env_suffix}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_lambda_function" "market_data" {
  for_each      = var.enable_market_data_lambdas ? local.market_data_jobs : {}
  function_name = "${var.project_name}-md-${each.key}${local.env_suffix}"
  role          = aws_iam_role.market_data.arn
  package_type  = "Image"
  image_uri     = local.market_data_image_uri
  memory_size   = 3008
  timeout       = 900

  environment {
    variables = {
      S3_BUCKET     = aws_s3_bucket.pipeline_data.id
      JOB           = each.value.job
      PYDATA_PREFIX = "pyData/Intermediate"
      UNIVERSE_KEY  = "universe/universe.csv"
      FRED_API_KEY  = var.fred_api_key
    }
  }

  depends_on = [aws_cloudwatch_log_group.market_data]
  tags       = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge schedules
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_event_rule" "market_data" {
  for_each            = local.market_data_jobs
  name                = "${var.project_name}-md-${each.key}-schedule${local.env_suffix}"
  description         = "Scheduled trigger for market-data job ${each.value.job}"
  schedule_expression = each.value.schedule
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "market_data" {
  for_each  = var.enable_market_data_lambdas ? local.market_data_jobs : {}
  rule      = aws_cloudwatch_event_rule.market_data[each.key].name
  target_id = "MarketDataLambda"
  arn       = aws_lambda_function.market_data[each.key].arn
}

resource "aws_lambda_permission" "market_data_eventbridge" {
  for_each      = var.enable_market_data_lambdas ? local.market_data_jobs : {}
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.market_data[each.key].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.market_data[each.key].arn
}
