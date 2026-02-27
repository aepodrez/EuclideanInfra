locals {
  cloudwatch_logs_s3_prefix_normalized = trim(var.cloudwatch_logs_s3_prefix, "/")
}

resource "aws_iam_role" "firehose_to_s3" {
  count = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  name  = "${var.project_name}-firehose-cwlogs-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "firehose.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "firehose_to_s3" {
  count = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  name  = "${var.project_name}-firehose-cwlogs-${var.environment}"
  role  = aws_iam_role.firehose_to_s3[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "s3:AbortMultipartUpload",
          "s3:GetBucketLocation",
          "s3:GetObject",
          "s3:ListBucket",
          "s3:ListBucketMultipartUploads",
          "s3:PutObject"
        ]
        Resource = [
          aws_s3_bucket.pipeline_data.arn,
          "${aws_s3_bucket.pipeline_data.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_kinesis_firehose_delivery_stream" "cloudwatch_to_s3" {
  count       = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  name        = "${var.project_name}-cwlogs-to-s3-${var.environment}"
  destination = "extended_s3"

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose_to_s3[0].arn
    bucket_arn          = aws_s3_bucket.pipeline_data.arn
    buffering_size      = 5
    buffering_interval  = 300
    compression_format  = "GZIP"
    prefix              = "${local.cloudwatch_logs_s3_prefix_normalized}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "${local.cloudwatch_logs_s3_prefix_normalized}/failed/!{firehose:error-output-type}/year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
  }

  tags = local.common_tags
}

resource "aws_iam_role" "cloudwatch_logs_to_firehose" {
  count = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  name  = "${var.project_name}-cwlogs-to-firehose-${var.environment}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "logs.${data.aws_region.current.name}.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy" "cloudwatch_logs_to_firehose" {
  count = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  name  = "${var.project_name}-cwlogs-to-firehose-${var.environment}"
  role  = aws_iam_role.cloudwatch_logs_to_firehose[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "firehose:PutRecord",
          "firehose:PutRecordBatch"
        ]
        Resource = aws_kinesis_firehose_delivery_stream.cloudwatch_to_s3[0].arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_account_policy" "all_logs_to_firehose" {
  count       = var.enable_cloudwatch_logs_archive_to_s3 ? 1 : 0
  policy_name = "${var.project_name}-cwlogs-to-s3-${var.environment}"
  policy_type = "SUBSCRIPTION_FILTER_POLICY"
  scope       = "ALL"

  policy_document = jsonencode({
    DestinationArn = aws_kinesis_firehose_delivery_stream.cloudwatch_to_s3[0].arn
    RoleArn        = aws_iam_role.cloudwatch_logs_to_firehose[0].arn
    FilterPattern  = ""
    Distribution   = "Random"
  })
}
