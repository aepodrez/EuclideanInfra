# Locals for constructing ARNs based on naming conventions
locals {
  alpha_model_lambda_arn             = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-alpha-model${local.env_suffix}"
  portfolio_construction_task_family = "${var.project_name}-portfolio-construction${local.env_suffix}"
  execution_model_lambda_arn         = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-execution-model${local.env_suffix}"

  # Predictor Lambda ARN prefix (functions: euclidean-pred-<name>). Built in DataIngressModel.
  pred_arn_prefix = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-pred-"

  # The 66 monthly predictor function-name suffixes (match euclidean-pred-<suffix>).
  predictor_names = [
    "accruals", "accrualsbm", "adexp", "ageipo", "assetgrowth", "bmdec", "beta",
    "bookleverage", "brandinvest", "cboperprof", "cf", "cash", "cashprod", "cheq",
    "chnwc", "chtax", "compequiss", "compositedebtissuance", "convdebt", "delcoa",
    "delcol", "deldrc", "divinit", "divseason", "earnsupbig", "earningsconsistency",
    "firmagemom", "gradexp", "grltnoa", "grsaletogrinv", "grsaletogroverhead", "high52",
    "indmom", "indretbig", "investppeinv", "ms", "mom12m", "mom12moffseason", "mom6m",
    "momoffseason", "momoffseason06yrplus", "momoffseason11yrplus", "momoffseason16yrplus",
    "momrev", "momseason", "momseason06yrplus", "momseason11yrplus", "momseason16yrplus",
    "momseasonshort", "momvol", "netdebtprice", "opleverage", "operprof", "ps", "rdability",
    "rdcap", "returnskew", "streversal", "sharevol", "trendfactor", "volmkt", "volsd",
    "volumetrend", "cfp", "dnoa", "std-turn",
  ]

  sfn_retry_lambda = [
    {
      ErrorEquals     = ["States.ALL"]
      IntervalSeconds = 10
      MaxAttempts     = 2
      BackoffRate     = 2.0
    }
  ]
  sfn_retry_ecs = [
    {
      ErrorEquals     = ["States.ALL"]
      IntervalSeconds = 20
      MaxAttempts     = 0
      BackoffRate     = 2.0
    }
  ]

  # One Parallel branch per predictor. Each invokes its Lambda; a failure is caught and
  # the branch passes (a single predictor with a data gap, e.g. OperProf's missing revt,
  # must not abort the whole run — the alpha model uses whatever predictor CSVs exist).
  predictor_branches = [
    for name in local.predictor_names : {
      StartAt = "Run_${name}"
      States = {
        "Run_${name}" = {
          Type     = "Task"
          Resource = "arn:aws:states:::lambda:invoke"
          Parameters = {
            FunctionName = "${local.pred_arn_prefix}${name}"
            Payload      = {}
          }
          Retry = local.sfn_retry_lambda
          Catch = [
            { ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "Skip_${name}" }
          ]
          ResultPath = null
          End        = true
        }
        "Skip_${name}" = { Type = "Pass", End = true }
      }
    }
  ]
}

# Step Functions State Machine — monthly decision pipeline.
# Data ingestion is handled separately by the overnight EventBridge Lambdas (market-data +
# edgar). This pipeline only makes decisions: build the SignalMasterTable, run the 66
# predictor Lambdas in parallel, then the alpha model and portfolio construction.
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline${local.env_suffix}"
  role_arn = aws_iam_role.step_function.arn

  definition = jsonencode({
    Comment = "Euclidean monthly decision pipeline: SignalMaster -> predictors -> alpha -> portfolio"
    StartAt = "RunSignalMaster"
    States = {
      # 1. Build SignalMasterTable.parquet (predictors depend on it).
      RunSignalMaster = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = "${local.pred_arn_prefix}signal-master"
          Payload      = {}
        }
        Retry      = local.sfn_retry_lambda
        ResultPath = "$.signal_master_result"
        Next       = "RunPredictors"
      }

      # 2. Run all 66 predictor Lambdas in parallel (fault-tolerant per branch).
      RunPredictors = {
        Type       = "Parallel"
        Branches   = local.predictor_branches
        ResultPath = "$.predictor_results"
        Next       = "RunAlphaModel"
      }

      # 3. Alpha model — reads predictor CSVs + universe, writes expected_returns.csv.
      # NOTE (Phase 4): the alpha handler derives prefixes as {data_ingress_prefix}/pyData/...
      # Our overnight layout writes predictors to pyData/Predictors and universe to
      # universe/universe.csv (no data-ingress prefix). The alpha handler needs a small
      # change to accept this root layout before this step succeeds end-to-end.
      RunAlphaModel = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.alpha_model_lambda_arn
          Payload = {
            s3_prefix           = "alpha-model"
            data_ingress_prefix = ""
            universe_path       = "universe/universe.csv"
            output_key          = "alpha-model/expected_returns.csv"
            "execution_id.$"    = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
          "row_count.$"      = "$.Payload.row_count"
          "used_fallback.$"  = "$.Payload.used_fallback"
          "warning.$"        = "$.Payload.warning"
        }
        Retry      = local.sfn_retry_lambda
        ResultPath = "$.alpha_model_result"
        Next       = "CheckAlphaModelResult"
      }

      CheckAlphaModelResult = {
        Type = "Choice"
        Choices = [
          {
            And = [
              { Variable = "$.alpha_model_result.statusCode", NumericEquals = 200 },
              { Variable = "$.alpha_model_result.used_fallback", BooleanEquals = false },
              { Variable = "$.alpha_model_result.row_count", NumericGreaterThan = 0 },
            ]
            Next = "RunPortfolioConstruction"
          }
        ]
        Default = "AlphaModelFailed"
      }

      AlphaModelFailed = {
        Type  = "Fail"
        Error = "AlphaModelFailed"
        Cause = "Alpha model failed, used fallback, or produced no rows."
      }

      # 4. Portfolio construction (ECS) — reads expected_returns + dailyCRSP + universe.
      RunPortfolioConstruction = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.portfolio_construction_task_family
          NetworkConfiguration = {
            AwsvpcConfiguration = {
              Subnets        = [for subnet in aws_subnet.public : subnet.id]
              SecurityGroups = [aws_security_group.ecs.id]
              AssignPublicIp = "ENABLED"
            }
          }
          Overrides = {
            ContainerOverrides = [
              {
                Name = "portfolio-construction"
                Environment = [
                  { Name = "S3_EXPECTED_RETURNS_KEY", "Value.$" = "$.alpha_model_result.s3_output_path" },
                  # New overnight layout prefixes (not the legacy data-ingress/ prefix).
                  { Name = "S3_UNIVERSE_KEY", Value = "universe/universe.csv" },
                  { Name = "S3_CRSP_PARQUET_KEY", Value = "pyData/Intermediate/dailyCRSP.parquet" },
                  { Name = "S3_TICKER_MAP_KEY", Value = "pyData/Intermediate/ticker_to_permno_monthly.csv" },
                  { Name = "S3_OUTPUT_KEY", Value = "portfolio-construction/optimal_portfolio.csv" },
                  { Name = "S3_OUTPUT_META_KEY", Value = "portfolio-construction/optimal_portfolio_meta.json" },
                ]
              }
            ]
          }
        }
        ResultSelector = {
          statusCode     = 200
          s3_output_path = "portfolio-construction/optimal_portfolio.csv"
          s3_meta_path   = "portfolio-construction/optimal_portfolio_meta.json"
        }
        Retry      = local.sfn_retry_ecs
        ResultPath = "$.portfolio_result"
        End        = true
      }
    }
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.step_function.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# EventBridge — trigger the decision pipeline monthly on the 1st at 08:30 UTC,
# after the overnight data jobs (crsp-monthly 05:30, edgar-ai-aggregator 08:00,
# signal-master-daily 08:15) have refreshed the inputs.
# ---------------------------------------------------------------------------
resource "aws_iam_role" "pipeline_scheduler" {
  name = "${var.project_name}-pipeline-scheduler${local.env_suffix}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Action = "sts:AssumeRole", Effect = "Allow", Principal = { Service = "events.amazonaws.com" } }]
  })
  tags = local.common_tags
}

resource "aws_iam_role_policy" "pipeline_scheduler" {
  name = "${var.project_name}-pipeline-scheduler${local.env_suffix}"
  role = aws_iam_role.pipeline_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["states:StartExecution"]
      Resource = [aws_sfn_state_machine.pipeline.arn]
    }]
  })
}

resource "aws_cloudwatch_event_rule" "pipeline_schedule" {
  name                = "${var.project_name}-pipeline-schedule${local.env_suffix}"
  description         = "Monthly trigger for the Euclidean decision pipeline (1st, 08:30 UTC)"
  schedule_expression = "cron(30 8 1 * ? *)"
  tags                = local.common_tags
}

resource "aws_cloudwatch_event_target" "pipeline_schedule" {
  rule     = aws_cloudwatch_event_rule.pipeline_schedule.name
  arn      = aws_sfn_state_machine.pipeline.arn
  role_arn = aws_iam_role.pipeline_scheduler.arn
}
