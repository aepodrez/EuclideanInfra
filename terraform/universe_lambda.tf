locals {
  universe_downloader_function_name = "${var.project_name}-universe-downloader${local.env_suffix}"
  universe_sic_worker_function_name = "${var.project_name}-universe-sic-worker${local.env_suffix}"
}

data "archive_file" "universe_downloader" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/universe_downloader"
  output_path = "${path.module}/.terraform/universe_downloader.zip"
}

data "archive_file" "universe_sic_worker" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/universe_sic_worker"
  output_path = "${path.module}/.terraform/universe_sic_worker.zip"
}

# ---------------------------------------------------------------------------
# CloudWatch log groups
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "universe_downloader" {
  name              = "/aws/lambda/${local.universe_downloader_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_cloudwatch_log_group" "universe_sic_worker" {
  name              = "/aws/lambda/${local.universe_sic_worker_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM — universe_downloader
# ---------------------------------------------------------------------------

resource "aws_iam_role" "universe_downloader" {
  name = "${var.project_name}-universe-downloader${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "universe_downloader_basic_logs" {
  role       = aws_iam_role.universe_downloader.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "universe_downloader" {
  name = "${var.project_name}-universe-downloader${local.env_suffix}"
  role = aws_iam_role.universe_downloader.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadUniverse"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/universe.csv"
        ]
      },
      {
        Sid    = "S3WriteWork"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/work/*"
        ]
      },
      {
        Sid    = "InvokeSicWorker"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.universe_sic_worker_function_name}"
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# IAM — universe_sic_worker
# ---------------------------------------------------------------------------

resource "aws_iam_role" "universe_sic_worker" {
  name = "${var.project_name}-universe-sic-worker${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "universe_sic_worker_basic_logs" {
  role       = aws_iam_role.universe_sic_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "universe_sic_worker" {
  name = "${var.project_name}-universe-sic-worker${local.env_suffix}"
  role = aws_iam_role.universe_sic_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWork"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/work/*"
        ]
      },
      {
        Sid    = "S3WriteWork"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/work/*"
        ]
      },
      {
        Sid    = "S3WriteUniverse"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/universe.csv"
        ]
      },
      {
        Sid    = "SelfInvoke"
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.universe_sic_worker_function_name}"
        ]
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Lambda functions
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "universe_downloader" {
  function_name    = local.universe_downloader_function_name
  role             = aws_iam_role.universe_downloader.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.universe_downloader.output_path
  source_code_hash = data.archive_file.universe_downloader.output_base64sha256
  memory_size      = 512
  timeout          = 60

  environment {
    variables = {
      S3_BUCKET                = aws_s3_bucket.pipeline_data.id
      UNIVERSE_KEY             = "universe/universe.csv"
      MANIFEST_PREFIX          = "universe/work"
      EDGAR_IDENTITY           = var.edgar_identity
      SIC_WORKER_FUNCTION_NAME = local.universe_sic_worker_function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.universe_downloader]
  tags       = local.common_tags
}

resource "aws_lambda_function" "universe_sic_worker" {
  function_name    = local.universe_sic_worker_function_name
  role             = aws_iam_role.universe_sic_worker.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.universe_sic_worker.output_path
  source_code_hash = data.archive_file.universe_sic_worker.output_base64sha256
  memory_size      = 512
  timeout          = 900

  environment {
    variables = {
      S3_BUCKET       = aws_s3_bucket.pipeline_data.id
      UNIVERSE_KEY    = "universe/universe.csv"
      MANIFEST_PREFIX = "universe/work"
      EDGAR_IDENTITY  = var.edgar_identity
    }
  }

  depends_on = [aws_cloudwatch_log_group.universe_sic_worker]
  tags       = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — trigger universe_downloader daily at 06:00 UTC
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "universe_downloader_schedule" {
  name                = "${var.project_name}-universe-downloader-schedule${local.env_suffix}"
  description         = "Daily trigger for universe_downloader at 06:00 UTC"
  schedule_expression = "cron(0 6 * * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "universe_downloader_schedule" {
  rule      = aws_cloudwatch_event_rule.universe_downloader_schedule.name
  target_id = "UniverseDownloaderLambda"
  arn       = aws_lambda_function.universe_downloader.arn
}

resource "aws_lambda_permission" "universe_downloader_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.universe_downloader.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.universe_downloader_schedule.arn
}
