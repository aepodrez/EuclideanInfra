# Locals for constructing ARNs based on naming conventions
locals {
  universe_lambda_arn                  = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-universe-${var.environment}"
  alpha_model_lambda_arn              = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-alpha-model-${var.environment}"
  portfolio_construction_lambda_arn   = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-portfolio-construction-${var.environment}"
  execution_model_lambda_arn           = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-execution-model-${var.environment}"
  data_ingress_task_family             = "${var.project_name}-data-ingress-${var.environment}"
}

# Step Functions State Machine
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline-${var.environment}"
  role_arn = aws_iam_role.step_function.arn

  definition = jsonencode({
    Comment = "Euclidean Trading Pipeline"
    StartAt = "RunUniverse"
    States  = {
      RunUniverse = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.universe_lambda_arn
          Payload = {
            s3_prefix                = "universe/"
            data_ingress_universe_key = "data-ingress/Static/universe.csv"
            "execution_id.$"         = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
        }
        ResultPath = "$.universe_result"
        Next       = "RunDataIngress"
      }
      RunDataIngress = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.data_ingress_task_family
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
                Name = "data-ingress"
                Environment = [
                  {
                    Name  = "S3_PREFIX"
                    Value = "data-ingress"
                  },
                  {
                    Name  = "UNIVERSE_PATH"
                    "Value.$" = "$.universe_result.s3_output_path"
                  },
                  {
                    Name  = "EXECUTION_ID"
                    "Value.$" = "$$.Execution.Id"
                  }
                ]
              }
            ]
          }
        }
        ResultSelector = {
          statusCode       = 200
          s3_output_prefix = "data-ingress"
        }
        ResultPath = "$.data_ingress_result"
        Next       = "RunAlphaModel"
      }
      RunAlphaModel = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.alpha_model_lambda_arn
          Payload = {
            s3_prefix              = "alpha-model"
            "universe_path.$"      = "$.universe_result.s3_output_path"
            "data_ingress_prefix.$" = "$.data_ingress_result.s3_output_prefix"
            output_key             = "alpha-model/expected_returns.csv"
            "execution_id.$"       = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
        }
        ResultPath = "$.alpha_model_result"
        Next       = "RunPortfolioConstruction"
      }
      RunPortfolioConstruction = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.portfolio_construction_lambda_arn
          Payload = {
            s3_prefix                = "portfolio-construction"
            "expected_returns_path.$" = "$.alpha_model_result.s3_output_path"
            universe_path            = "data-ingress/Static/universe.csv"
            daily_crsp_path          = "data-ingress/pyData/Intermediate/dailyCRSP.parquet"
            ticker_map_path          = "data-ingress/pyData/Intermediate/ticker_to_permno.csv"
            output_key               = "portfolio-construction/optimal_portfolio.csv"
            meta_key                 = "portfolio-construction/optimal_portfolio_meta.json"
            "execution_id.$"         = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
        }
        ResultPath = "$.portfolio_result"
        Next       = "RunExecutionModel"
      }
      RunExecutionModel = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.execution_model_lambda_arn
          Payload = {
            s3_prefix                  = "execution-model"
            "portfolio_weights_path.$" = "$.portfolio_result.s3_output_path"
            output_key                 = "execution-model/execution_report.json"
            dry_run                    = true
            "execution_id.$"           = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
        }
        ResultPath = "$.execution_result"
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
