locals {
  edgar_filing_poller_function_name = "${var.project_name}-edgar-filing-poller${local.env_suffix}"
}

data "archive_file" "edgar_filing_poller" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/edgar_filing_poller"
  output_path = "${path.module}/.terraform/edgar_filing_poller.zip"
}

resource "aws_cloudwatch_log_group" "edgar_filing_poller" {
  name              = "/aws/lambda/${local.edgar_filing_poller_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "edgar_filing_poller" {
  name = "${var.project_name}-edgar-filing-poller${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "edgar_filing_poller_basic_logs" {
  role       = aws_iam_role.edgar_filing_poller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "edgar_filing_poller" {
  name = "${var.project_name}-edgar-filing-poller${local.env_suffix}"
  role = aws_iam_role.edgar_filing_poller.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3Universe"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/universe/universe.csv"
        ]
      },
      {
        Sid    = "S3HeadFilings"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/filings/*"
        ]
      },
      {
        Sid    = "S3ListFilings"
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.pipeline_data.arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["data-ingress/filings/*"]
          }
        }
      },
      {
        Sid    = "SQSPublish"
        Effect = "Allow"
        Action = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.edgar_filings.arn]
      },
    ]
  })
}

resource "aws_lambda_function" "edgar_filing_poller" {
  function_name    = local.edgar_filing_poller_function_name
  role             = aws_iam_role.edgar_filing_poller.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.edgar_filing_poller.output_path
  source_code_hash = data.archive_file.edgar_filing_poller.output_base64sha256
  memory_size      = 1024
  timeout          = 900

  environment {
    variables = {
      S3_BUCKET      = aws_s3_bucket.pipeline_data.id
      UNIVERSE_KEY   = "universe/universe.csv"
      SQS_QUEUE_URL  = aws_sqs_queue.edgar_filings.url
      EDGAR_IDENTITY = var.edgar_identity
      LOOKBACK_DAYS  = "365"
    }
  }

  depends_on = [aws_cloudwatch_log_group.edgar_filing_poller]
  tags       = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — trigger filing poller daily at 09:00 UTC
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "edgar_filing_poller_schedule" {
  name                = "${var.project_name}-edgar-filing-poller-schedule${local.env_suffix}"
  description         = "Daily trigger for edgar_filing_poller at 06:30 UTC (30 min after universe_downloader)"
  schedule_expression = "cron(30 6 * * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "edgar_filing_poller_schedule" {
  rule      = aws_cloudwatch_event_rule.edgar_filing_poller_schedule.name
  target_id = "EdgarFilingPollerLambda"
  arn       = aws_lambda_function.edgar_filing_poller.arn
}

resource "aws_lambda_permission" "edgar_filing_poller_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.edgar_filing_poller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.edgar_filing_poller_schedule.arn
}
