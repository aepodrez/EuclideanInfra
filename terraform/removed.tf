# ---------------------------------------------------------------------------
# Migration removed blocks — release edgar pipeline + universe Lambdas from
# this workspace's state WITHOUT destroying them. They are adopted by the
# DataIngressModel and UniverseModel workspaces via import blocks.
#
# Safe to delete after the first successful apply.
# (market_data resources intentionally NOT removed — staying here for now.)
# ---------------------------------------------------------------------------

# --- SQS ---
removed {
  from = aws_sqs_queue.edgar_filings
  lifecycle { destroy = false }
}
removed {
  from = aws_sqs_queue.edgar_filings_dlq
  lifecycle { destroy = false }
}

# --- edgar_filing_poller ---
removed {
  from = aws_cloudwatch_log_group.edgar_filing_poller
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role.edgar_filing_poller
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.edgar_filing_poller_basic_logs
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy.edgar_filing_poller
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_function.edgar_filing_poller
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_rule.edgar_filing_poller_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_target.edgar_filing_poller_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_permission.edgar_filing_poller_eventbridge
  lifecycle { destroy = false }
}

# --- edgar_ai_worker ---
removed {
  from = aws_cloudwatch_log_group.edgar_ai_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role.edgar_ai_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.edgar_ai_worker_basic_logs
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy.edgar_ai_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_function.edgar_ai_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_event_source_mapping.edgar_ai_worker_sqs
  lifecycle { destroy = false }
}

# --- edgar_ai_aggregator ---
removed {
  from = aws_cloudwatch_log_group.edgar_ai_aggregator
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role.edgar_ai_aggregator
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.edgar_ai_aggregator_basic_logs
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy.edgar_ai_aggregator
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_function.edgar_ai_aggregator
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_rule.edgar_ai_aggregator_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_target.edgar_ai_aggregator_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_permission.edgar_ai_aggregator_eventbridge
  lifecycle { destroy = false }
}

# --- universe_downloader ---
removed {
  from = aws_cloudwatch_log_group.universe_downloader
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role.universe_downloader
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.universe_downloader_basic_logs
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy.universe_downloader
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_function.universe_downloader
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_rule.universe_downloader_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_cloudwatch_event_target.universe_downloader_schedule
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_permission.universe_downloader_eventbridge
  lifecycle { destroy = false }
}

# --- universe_sic_worker ---
removed {
  from = aws_cloudwatch_log_group.universe_sic_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role.universe_sic_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy_attachment.universe_sic_worker_basic_logs
  lifecycle { destroy = false }
}
removed {
  from = aws_iam_role_policy.universe_sic_worker
  lifecycle { destroy = false }
}
removed {
  from = aws_lambda_function.universe_sic_worker
  lifecycle { destroy = false }
}
