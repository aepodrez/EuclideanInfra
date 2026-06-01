locals {
  edgar_filings_queue_name = "${var.project_name}-edgar-filings${local.env_suffix}"
  edgar_filings_dlq_name   = "${var.project_name}-edgar-filings-dlq${local.env_suffix}"
}

resource "aws_sqs_queue" "edgar_filings_dlq" {
  name                      = local.edgar_filings_dlq_name
  message_retention_seconds = 1209600 # 14 days
  tags                      = local.common_tags
}

resource "aws_sqs_queue" "edgar_filings" {
  name                       = local.edgar_filings_queue_name
  visibility_timeout_seconds = 360 # must be >= Lambda timeout (300s) + buffer
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20 # long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.edgar_filings_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.common_tags
}
