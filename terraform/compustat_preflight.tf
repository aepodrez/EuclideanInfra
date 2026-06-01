locals {
  compustat_preflight_function_name = "${var.project_name}-compustat-preflight${local.env_suffix}"
  compustat_preflight_lambda_arn    = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${local.compustat_preflight_function_name}"
}

data "archive_file" "compustat_preflight" {
  type        = "zip"
  source_dir  = "${path.module}/../lambdas/compustat_preflight"
  output_path = "${path.module}/.terraform/compustat_preflight.zip"
}

resource "aws_cloudwatch_log_group" "compustat_preflight" {
  name              = "/aws/lambda/${local.compustat_preflight_function_name}"
  retention_in_days = 30
  tags              = local.common_tags
}

resource "aws_iam_role" "compustat_preflight" {
  name = "${var.project_name}-compustat-preflight${local.env_suffix}"

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

resource "aws_iam_role_policy_attachment" "compustat_preflight_basic_logs" {
  role       = aws_iam_role.compustat_preflight.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "compustat_preflight_s3" {
  name = "${var.project_name}-compustat-preflight-s3${local.env_suffix}"
  role = aws_iam_role.compustat_preflight.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/Static/universe.csv",
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/cache/compustat_preflight/*",
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.pipeline_data.arn}/data-ingress/cache/compustat_preflight/*",
        ]
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

resource "aws_lambda_function" "compustat_preflight" {
  function_name    = local.compustat_preflight_function_name
  role             = aws_iam_role.compustat_preflight.arn
  runtime          = "python3.12"
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.compustat_preflight.output_path
  source_code_hash = data.archive_file.compustat_preflight.output_base64sha256
  memory_size      = 512
  timeout          = 120

  environment {
    variables = {
      S3_BUCKET      = aws_s3_bucket.pipeline_data.id
      UNIVERSE_KEY   = "data-ingress/Static/universe.csv"
      MANIFEST_KEY   = "data-ingress/cache/compustat_preflight/manifest.json"
      EDGAR_IDENTITY = var.edgar_identity
      MAX_SKIP_DAYS  = "7"
      SQS_QUEUE_URL  = aws_sqs_queue.edgar_filings.url
    }
  }

  depends_on = [aws_cloudwatch_log_group.compustat_preflight]
  tags       = local.common_tags
}
