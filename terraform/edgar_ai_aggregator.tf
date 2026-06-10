locals {
  edgar_ai_aggregator_function_name = "${var.project_name}-edgar-ai-aggregator${local.env_suffix}"
}

data "archive_file" "edgar_ai_aggregator" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/edgar_ai_aggregator"
  output_path = "${path.module}/.terraform/edgar_ai_aggregator.zip"
}

resource "aws_cloudwatch_log_group" "edgar_ai_aggregator" {
  name              = "/aws/lambda/${local.edgar_ai_aggregator_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "edgar_ai_aggregator" {
  name = "${var.project_name}-edgar-ai-aggregator${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "edgar_ai_aggregator_basic_logs" {
  role       = aws_iam_role.edgar_ai_aggregator.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "edgar_ai_aggregator" {
  name = "${var.project_name}-edgar-ai-aggregator${local.env_suffix}"
  role = aws_iam_role.edgar_ai_aggregator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadFilings"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:HeadObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/filings/*"
        ]
      },
      {
        Sid    = "S3ListFilingsAndPyData"
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = [aws_s3_bucket.pipeline_data.arn]
        Condition = {
          StringLike = {
            "s3:prefix" = ["data-ingress/filings/*", "pyData/Intermediate/*"]
          }
        }
      },
      {
        Sid    = "S3ReadWritePyData"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:HeadObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/pyData/Intermediate/*"
        ]
      },
      {
        Sid    = "S3WatermarkReadWrite"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/filings/.last_aggregated"
        ]
      },
    ]
  })
}

resource "aws_lambda_function" "edgar_ai_aggregator" {
  function_name    = local.edgar_ai_aggregator_function_name
  role             = aws_iam_role.edgar_ai_aggregator.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.edgar_ai_aggregator.output_path
  source_code_hash = data.archive_file.edgar_ai_aggregator.output_base64sha256
  memory_size      = 3008   # Lambda max — enough for incremental runs; full rebuild batches in pandas
  timeout          = 900    # 15 min

  layers = [local.aws_sdk_pandas_layer_arn]

  environment {
    variables = {
      S3_BUCKET     = aws_s3_bucket.pipeline_data.id
      PYDATA_PREFIX = "pyData/Intermediate"
    }
  }

  depends_on = [aws_cloudwatch_log_group.edgar_ai_aggregator]
  tags       = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — trigger daily at 08:00 UTC (after edgar_ai_worker overnight run)
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "edgar_ai_aggregator_schedule" {
  name                = "${var.project_name}-edgar-ai-aggregator-schedule${local.env_suffix}"
  description         = "Daily trigger for edgar_ai_aggregator at 08:00 UTC"
  schedule_expression = "cron(0 8 * * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "edgar_ai_aggregator_schedule" {
  rule      = aws_cloudwatch_event_rule.edgar_ai_aggregator_schedule.name
  target_id = "EdgarAiAggregatorLambda"
  arn       = aws_lambda_function.edgar_ai_aggregator.arn
}

resource "aws_lambda_permission" "edgar_ai_aggregator_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.edgar_ai_aggregator.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.edgar_ai_aggregator_schedule.arn
}
