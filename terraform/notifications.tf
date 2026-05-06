resource "aws_sns_topic" "pipeline_notifications" {
  name = "${var.project_name}-pipeline-notifications${local.env_suffix}"
}

resource "aws_sns_topic_policy" "pipeline_notifications" {
  arn = aws_sns_topic.pipeline_notifications.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEventBridgePublish"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.pipeline_notifications.arn
    }]
  })
}

resource "aws_sns_topic_subscription" "sms" {
  topic_arn = aws_sns_topic.pipeline_notifications.arn
  protocol  = "sms"
  endpoint  = var.notification_phone_number
}

resource "aws_cloudwatch_event_rule" "pipeline_state_change" {
  name        = "${var.project_name}-pipeline-state-change${local.env_suffix}"
  description = "Fires on Step Functions pipeline SUCCEEDED, FAILED, TIMED_OUT, or ABORTED"

  event_pattern = jsonencode({
    source        = ["aws.states"]
    "detail-type" = ["Step Functions Execution Status Change"]
    detail = {
      stateMachineArn = [aws_sfn_state_machine.pipeline.arn]
      status          = ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "pipeline_sms" {
  rule      = aws_cloudwatch_event_rule.pipeline_state_change.name
  target_id = "PipelineSNS"
  arn       = aws_sns_topic.pipeline_notifications.arn

  input_transformer {
    input_paths = {
      status = "$.detail.status"
      name   = "$.detail.name"
    }
    input_template = "\"Euclidean pipeline <name>: <status>\""
  }
}
