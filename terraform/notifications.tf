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
  # Dormant (notification_phone_number defaults to "" -> count=0). SMS requires
  # toll-free/10DLC carrier registration, which needs a verifiable company
  # (registered business, matching-domain website + support email) - doesn't
  # fit a single-recipient personal alert. Switched to email below instead.
  count     = var.notification_phone_number != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_notifications.arn
  protocol  = "sms"
  endpoint  = var.notification_phone_number
}

resource "aws_sns_topic_subscription" "email" {
  # AWS emails a confirmation link to notification_email after apply - it must
  # be clicked before this subscription becomes Confirmed and starts receiving
  # notifications (SNS subscriptions sit in PendingConfirmation until then).
  count     = var.notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_notifications.arn
  protocol  = "email"
  endpoint  = var.notification_email
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
  # Name is a holdover from the original SMS-only design - this just publishes
  # to the SNS topic and is agnostic to which protocol(s) are subscribed
  # (email now, SMS if ever revisited). Left as-is to avoid an unnecessary
  # destroy/recreate for a cosmetic rename.
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
