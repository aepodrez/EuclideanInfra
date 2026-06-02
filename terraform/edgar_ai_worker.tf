locals {
  edgar_ai_worker_function_name = "${var.project_name}-edgar-ai-worker${local.env_suffix}"

  # AWSSDKPandas managed layer (includes pyarrow). Update version periodically.
  # https://aws-sdk-pandas.readthedocs.io/en/stable/layers.html
  aws_sdk_pandas_layer_arn = "arn:aws:lambda:${data.aws_region.current.name}:336392948345:layer:AWSSDKPandas-Python312:16"
}

data "archive_file" "edgar_ai_worker" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/edgar_ai_worker"
  output_path = "${path.module}/.terraform/edgar_ai_worker.zip"
}

resource "aws_cloudwatch_log_group" "edgar_ai_worker" {
  name              = "/aws/lambda/${local.edgar_ai_worker_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "edgar_ai_worker" {
  name = "${var.project_name}-edgar-ai-worker${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "edgar_ai_worker_basic_logs" {
  role       = aws_iam_role.edgar_ai_worker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "edgar_ai_worker" {
  name = "${var.project_name}-edgar-ai-worker${local.env_suffix}"
  role = aws_iam_role.edgar_ai_worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3WriteFilings"
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/filings/*"
        ]
      },
      {
        Sid    = "SQSConsume"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        Resource = [aws_sqs_queue.edgar_filings.arn]
      },
    ]
  })
}

resource "aws_lambda_function" "edgar_ai_worker" {
  function_name    = local.edgar_ai_worker_function_name
  role             = aws_iam_role.edgar_ai_worker.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.edgar_ai_worker.output_path
  source_code_hash = data.archive_file.edgar_ai_worker.output_base64sha256
  memory_size      = 1536
  timeout          = 900

  layers = [local.aws_sdk_pandas_layer_arn]

  environment {
    variables = {
      S3_BUCKET         = aws_s3_bucket.pipeline_data.id
      OPENROUTER_API_KEY = var.openrouter_api_key
    }
  }

  depends_on = [aws_cloudwatch_log_group.edgar_ai_worker]
  tags       = local.common_tags
}

resource "aws_lambda_event_source_mapping" "edgar_ai_worker_sqs" {
  event_source_arn                   = aws_sqs_queue.edgar_filings.arn
  function_name                      = aws_lambda_function.edgar_ai_worker.arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 10
  }
}
