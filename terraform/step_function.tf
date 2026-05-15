# Locals for constructing ARNs based on naming conventions
locals {
  universe_task_family                         = "${var.project_name}-universe${local.env_suffix}"
  alpha_model_lambda_arn                       = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-alpha-model${local.env_suffix}"
  portfolio_construction_task_family           = "${var.project_name}-portfolio-construction${local.env_suffix}"
  execution_model_lambda_arn                   = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-execution-model${local.env_suffix}"
  data_ingress_downloads_task_family           = "${var.project_name}-data-ingress-downloads${local.env_suffix}"
  data_ingress_compustat_annual_task_family    = "${var.project_name}-data-ingress-compustat-annual${local.env_suffix}"
  data_ingress_compustat_quarterly_task_family = "${var.project_name}-data-ingress-compustat-quarterly${local.env_suffix}"
  data_ingress_refinitiv_task_family           = "${var.project_name}-data-ingress-refinitiv${local.env_suffix}"
  data_ingress_predictors_task_family          = "${var.project_name}-data-ingress-predictors${local.env_suffix}"
  sfn_retry_ecs = [
    {
      ErrorEquals     = ["States.ALL"]
      IntervalSeconds = 20
      MaxAttempts     = 0
      BackoffRate     = 2.0
    }
  ]
  sfn_retry_lambda = [
    {
      ErrorEquals     = ["States.ALL"]
      IntervalSeconds = 10
      MaxAttempts     = 2
      BackoffRate     = 2.0
    }
  ]
}

# Step Functions State Machine
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline${local.env_suffix}"
  role_arn = aws_iam_role.step_function.arn

  definition = jsonencode({
    Comment = "Euclidean Trading Pipeline"
    StartAt = "RunUniverse"
    States = {
      RunUniverse = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.universe_task_family
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
                Name = "universe"
                Environment = [
                  {
                    Name  = "S3_PREFIX"
                    Value = "universe"
                  },
                  {
                    Name  = "DATA_INGRESS_UNIVERSE_KEY"
                    Value = "data-ingress/Static/universe.csv"
                  },
                  {
                    Name      = "EXECUTION_ID"
                    "Value.$" = "$$.Execution.Id"
                  },
                  {
                    Name      = "STEP_FUNCTION_STATE_NAME"
                    "Value.$" = "$$.State.Name"
                  }
                ]
              }
            ]
          }
        }
        ResultSelector = {
          statusCode     = 200
          s3_output_path = "universe/universe.csv"
        }
        Retry      = local.sfn_retry_ecs
        ResultPath = "$.universe_result"
        Next       = "RunDataIngressJobGraph"
      }
      RunDataIngressJobGraph = {
        Type = "Parallel"
        Branches = [
          # Branch 1: CRSP critical path (CRSPMonthly must precede Acquisitions and IPODates)
          {
            StartAt = "RunCRSPMonthly"
            States = {
              RunCRSPMonthly = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCRSPMonthly" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CRSPMonthly.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "PostCRSPParallel"
              }
              PostCRSPParallel = {
                Type = "Parallel"
                Branches = [
                  {
                    StartAt = "RunCRSPAcquisitions"
                    States = {
                      RunCRSPAcquisitions = {
                        Type     = "Task"
                        Resource = "arn:aws:states:::ecs:runTask.sync"
                        Parameters = {
                          LaunchType     = "FARGATE"
                          Cluster        = aws_ecs_cluster.main.arn
                          TaskDefinition = local.data_ingress_downloads_task_family
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
                                Name = "crosssection-data"
                                Environment = [
                                  { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                                  { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                                  { Name = "CROSSSECTION_JOB_NAME", Value = "RunCRSPAcquisitions" },
                                  { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CRSPAcquisitions.py" },
                                  { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                                  { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\"]" },
                                  { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\"]" },
                                  { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/m_CRSPAcquisitions.parquet\"]" },
                                  { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/m_CRSPAcquisitions.parquet\"]" },
                                ]
                              }
                            ]
                          }
                        }
                        ResultSelector = { statusCode = 200 }
                        Retry          = local.sfn_retry_ecs
                        End            = true
                      }
                    }
                  },
                  {
                    StartAt = "RunIPODates"
                    States = {
                      RunIPODates = {
                        Type     = "Task"
                        Resource = "arn:aws:states:::ecs:runTask.sync"
                        Parameters = {
                          LaunchType     = "FARGATE"
                          Cluster        = aws_ecs_cluster.main.arn
                          TaskDefinition = local.data_ingress_downloads_task_family
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
                                Name = "crosssection-data"
                                Environment = [
                                  { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                                  { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                                  { Name = "CROSSSECTION_JOB_NAME", Value = "RunIPODates" },
                                  { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/IPODates.py" },
                                  { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                                  { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\"]" },
                                  { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                                  { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IPODates.parquet\",\"pyData/Intermediate/IPODates.csv\"]" },
                                  { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IPODates.parquet\"]" },
                                ]
                              }
                            ]
                          }
                        }
                        ResultSelector = { statusCode = 200 }
                        Retry          = local.sfn_retry_ecs
                        End            = true
                      }
                    }
                  }
                ]
                End = true
              }
            }
          },
          # Branch 2: Refinitiv (sequential — API rate limits)
          {
            StartAt = "RunIBESEPSAdjusted"
            States = {
              RunIBESEPSAdjusted = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_refinitiv_task_family
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
                        Name = "crosssection-refinitiv"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunIBESEPSAdjusted" },
                          { Name = "CROSSSECTION_REFINITIV_SCRIPTS", Value = "DataDownloads/IBESEPSAdjusted.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\", \"pyData/Intermediate/IBES_EPS_Adj.parquet\", \"pyData/Intermediate/IBES_EPS_Adj.checkpoint.parquet\", \"pyData/Intermediate/IBES_EPS_Adj.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBES_EPS_Adj.parquet\", \"pyData/Intermediate/IBES_EPS_Adj.checkpoint.parquet\", \"pyData/Intermediate/IBES_EPS_Adj.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBES_EPS_Adj.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunIBESEPSUnadjusted"
              }
              RunIBESEPSUnadjusted = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_refinitiv_task_family
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
                        Name = "crosssection-refinitiv"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunIBESEPSUnadjusted" },
                          { Name = "CROSSSECTION_REFINITIV_SCRIPTS", Value = "DataDownloads/IBESEPSUnadjusted.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/IBES_EPS_Unadj.parquet\",\"pyData/Intermediate/IBES_EPS_Unadj.checkpoint.parquet\",\"pyData/Intermediate/IBES_EPS_Unadj.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBES_EPS_Unadj.parquet\",\"pyData/Intermediate/IBES_EPS_Unadj.checkpoint.parquet\",\"pyData/Intermediate/IBES_EPS_Unadj.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBES_EPS_Unadj.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunIBESUnadjustedActuals"
              }
              RunIBESUnadjustedActuals = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_refinitiv_task_family
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
                        Name = "crosssection-refinitiv"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunIBESUnadjustedActuals" },
                          { Name = "CROSSSECTION_REFINITIV_SCRIPTS", Value = "DataDownloads/IBESUnadjustedActuals.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/IBES_UnadjustedActuals.parquet\",\"pyData/Intermediate/IBES_UnadjustedActuals.checkpoint.parquet\",\"pyData/Intermediate/IBES_UnadjustedActuals.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBES_UnadjustedActuals.parquet\",\"pyData/Intermediate/IBES_UnadjustedActuals.checkpoint.parquet\",\"pyData/Intermediate/IBES_UnadjustedActuals.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBES_UnadjustedActuals.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunIBESRecommendations"
              }
              RunIBESRecommendations = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_refinitiv_task_family
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
                        Name = "crosssection-refinitiv"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunIBESRecommendations" },
                          { Name = "CROSSSECTION_REFINITIV_SCRIPTS", Value = "DataDownloads/IBESRecommendations.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/IBES_Recommendations.parquet\",\"pyData/Intermediate/IBES_Recommendations.checkpoint.parquet\",\"pyData/Intermediate/IBES_Recommendations.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBES_Recommendations.parquet\",\"pyData/Intermediate/IBES_Recommendations.checkpoint.parquet\",\"pyData/Intermediate/IBES_Recommendations.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBES_Recommendations.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 3: CRSPDaily (independent)
          {
            StartAt = "RunCRSPDaily"
            States = {
              RunCRSPDaily = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCRSPDaily" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CRSPDaily.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/dailyCRSP.parquet\",\"pyData/Intermediate/dailyCRSPprc.parquet\",\"pyData/Intermediate/cache/crsp_daily/\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/dailyCRSP.parquet\",\"pyData/Intermediate/dailyCRSPprc.parquet\",\"pyData/Intermediate/cache/crsp_daily/\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/dailyCRSP.parquet\",\"pyData/Intermediate/dailyCRSPprc.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 4: CRSPDistributions (independent)
          {
            StartAt = "RunCRSPDistributions"
            States = {
              RunCRSPDistributions = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCRSPDistributions" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CRSPDistributions.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/CRSPdistributions.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/CRSPdistributions.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 5: BEAInputOutput (independent)
          {
            StartAt = "RunBEAInputOutput"
            States = {
              RunBEAInputOutput = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunBEAInputOutput" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/BEAInputOutput.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/BEA_Supply_Table.parquet\",\"pyData/Intermediate/BEA_Supply_Table.csv\",\"pyData/Intermediate/BEA_SupplyUse_Framework.parquet\",\"pyData/Intermediate/BEA_SupplyUse_Framework.csv\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/BEA_Supply_Table.parquet\",\"pyData/Intermediate/BEA_SupplyUse_Framework.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 6: CompustatAnnual (dedicated task family — larger instance)
          {
            StartAt = "RunCompustatAnnual"
            States = {
              RunCompustatAnnual = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_compustat_annual_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCompustatAnnual" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CompustatAnnual.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[\"--universe_csv\",\"../Static/universe.csv\",\"--output_dir\",\"../pyData/Intermediate/compustat_annual\"]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/compustat_annual/outputs/features.parquet\",\"pyData/Intermediate/compustat_annual/outputs/features.csv\",\"pyData/Intermediate/compustat_annual/outputs/diagnostics_anchor_residuals.parquet\",\"pyData/Intermediate/a_aCompustat.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/a_aCompustat.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 7: CompustatQuarterly (dedicated task family — larger instance)
          {
            StartAt = "RunCompustatQuarterly"
            States = {
              RunCompustatQuarterly = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_compustat_quarterly_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCompustatQuarterly" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CompustatQuarterly.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[\"--universe_csv\",\"../Static/universe.csv\",\"--output_dir\",\"../pyData/Intermediate/compustat_quarterly\"]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/compustat_quarterly/outputs/quarterly_features.parquet\",\"pyData/Intermediate/compustat_quarterly/outputs/quarterly_features.csv\",\"pyData/Intermediate/compustat_quarterly/outputs/quarterly_diagnostics_anchor_residuals.parquet\",\"pyData/Intermediate/m_QCompustat.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/m_QCompustat.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 8: GNPDeflator (independent)
          {
            StartAt = "RunGNPDeflator"
            States = {
              RunGNPDeflator = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunGNPDeflator" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/GNPDeflator.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/GNPdefl.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/GNPdefl.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 9: InstitutionalHoldings13F (independent, larger memory)
          {
            StartAt = "RunInstitutionalHoldings13F"
            States = {
              RunInstitutionalHoldings13F = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
                  NetworkConfiguration = {
                    AwsvpcConfiguration = {
                      Subnets        = [for subnet in aws_subnet.public : subnet.id]
                      SecurityGroups = [aws_security_group.ecs.id]
                      AssignPublicIp = "ENABLED"
                    }
                  }
                  Overrides = {
                    Cpu    = "2048"
                    Memory = "16384"
                    ContainerOverrides = [
                      {
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunInstitutionalHoldings13F" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/InstitutionalHoldings13F.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/TR_13F.parquet\",\"pyData/Intermediate/.cache/13F_holdings_cache.parquet\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/TR_13F.parquet\",\"pyData/Intermediate/.cache/13F_holdings_cache.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/TR_13F.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 10: MarketReturns (independent)
          {
            StartAt = "RunMarketReturns"
            States = {
              RunMarketReturns = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunMarketReturns" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/MarketReturns.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyMarket.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/monthlyMarket.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 11: QFactorModel (independent)
          {
            StartAt = "RunQFactorModel"
            States = {
              RunQFactorModel = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunQFactorModel" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/QFactorModel.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/d_qfactor_live.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/d_qfactor_live.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 12: TreasuryBill3M (independent)
          {
            StartAt = "RunTreasuryBill3M"
            States = {
              RunTreasuryBill3M = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunTreasuryBill3M" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/TreasuryBill3M.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/TBill3M.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/TBill3M.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 13: VIX (independent)
          {
            StartAt = "RunVIX"
            States = {
              RunVIX = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunVIX" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/VIX.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/d_vix.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/d_vix.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 14: BaliHovak implied volatility (Yahoo Finance options, incremental snapshots)
          {
            StartAt = "RunBaliHovak"
            States = {
              RunBaliHovak = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunBaliHovak" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/BaliHovak.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/bali_hovak_imp_vol.parquet\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/bali_hovak_imp_vol.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 15: OptionMetricsVolume (Yahoo Finance options, incremental snapshots)
          {
            StartAt = "RunOptionMetricsVolume"
            States = {
              RunOptionMetricsVolume = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunOptionMetricsVolume" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/OptionMetricsVolume.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/OptionMetricsVolume.parquet\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/OptionMetricsVolume.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 16: OptionMetricsXZZ (Yahoo Finance options, incremental snapshots)
          {
            StartAt = "RunOptionMetricsXZZ"
            States = {
              RunOptionMetricsXZZ = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunOptionMetricsXZZ" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/OptionMetricsXZZ.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/OptionMetricsXZZ.parquet\",\"pyData/OptionSnapshots/\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/OptionMetricsXZZ.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 17: CorwinSchultz bid-ask spread (Yahoo Finance OHLC)
          {
            StartAt = "RunCorwinSchultz"
            States = {
              RunCorwinSchultz = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCorwinSchultz" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CorwinSchultz.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/corwin_schultz_spread.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/corwin_schultz_spread.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 18: CompustatBusinessSegments (SEC EDGAR XBRL)
          {
            StartAt = "RunCompustatBusinessSegments"
            States = {
              RunCompustatBusinessSegments = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCompustatBusinessSegments" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CompustatBusinessSegments.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/CompustatSegments.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/CompustatSegments.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 19: CompustatCustomerSegments (SEC EDGAR XBRL)
          {
            StartAt = "RunCompustatCustomerSegments"
            States = {
              RunCompustatCustomerSegments = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCompustatCustomerSegments" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CompustatCustomerSegments.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/CompustatSegmentDataCustomers.csv\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/CompustatSegmentDataCustomers.csv\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # Branch 20: Fama-French (BuildFFPortfolios must precede daily then monthly)
          {
            StartAt = "RunBuildFFPortfolios"
            States = {
              RunBuildFFPortfolios = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunBuildFFPortfolios" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/BuildFFPortfolios.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\",\"pyData/Intermediate/cache/build_ff_portfolios/\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/universe.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"Static/ff3_portfolios.csv\",\"pyData/Intermediate/cache/build_ff_portfolios/\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"Static/ff3_portfolios.csv\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunFamaFrenchDaily"
              }
              RunFamaFrenchDaily = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunFamaFrenchDaily" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/FamaFrenchDaily.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/ff3_portfolios.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/ff3_portfolios.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/dailyFF.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/dailyFF.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunFamaFrenchMonthly"
              }
              RunFamaFrenchMonthly = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunFamaFrenchMonthly" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/FamaFrenchMonthly.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/ff3_portfolios.csv\",\"pyData/Intermediate/monthlyFF.parquet\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"Static/ff3_portfolios.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyFF.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/monthlyFF.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          }
        ]
        ResultPath = "$.data_ingress_job_graph_result"
        Next       = "RunDataIngressLinking"
      }
      # Phase 2: Jobs that depend on Phase 1 outputs (run in parallel with each other)
      RunDataIngressLinking = {
        Type = "Parallel"
        Branches = [
          # CompustatShortInterest: needs m_aCompustat.parquet from RunCompustatAnnual
          {
            StartAt = "RunCompustatShortInterest"
            States = {
              RunCompustatShortInterest = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunCompustatShortInterest" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CompustatShortInterest.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/m_aCompustat.parquet\",\"pyData/Intermediate/monthlyShortInterest.parquet\",\"pyData/Intermediate/monthlyShortInterest.checkpoint.parquet\",\"pyData/Intermediate/monthlyShortInterest.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/m_aCompustat.parquet\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyShortInterest.parquet\",\"pyData/Intermediate/monthlyShortInterest.checkpoint.parquet\",\"pyData/Intermediate/monthlyShortInterest.checkpoint.json\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/monthlyShortInterest.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # IBESCRSPLink: needs monthlyCRSP.parquet + IBES_EPS_Adj.parquet
          {
            StartAt = "RunIBESCRSPLink"
            States = {
              RunIBESCRSPLink = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
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
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunIBESCRSPLink" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/IBESCRSPLink.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/IBES_EPS_Adj.parquet\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/IBES_EPS_Adj.parquet\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          },
          # USASpending: needs a_aCompustat.parquet from RunCompustatAnnual
          {
            StartAt = "RunUSASpending"
            States = {
              RunUSASpending = {
                Type     = "Task"
                Resource = "arn:aws:states:::ecs:runTask.sync"
                Parameters = {
                  LaunchType     = "FARGATE"
                  Cluster        = aws_ecs_cluster.main.arn
                  TaskDefinition = local.data_ingress_downloads_task_family
                  NetworkConfiguration = {
                    AwsvpcConfiguration = {
                      Subnets        = [for subnet in aws_subnet.public : subnet.id]
                      SecurityGroups = [aws_security_group.ecs.id]
                      AssignPublicIp = "ENABLED"
                    }
                  }
                  Overrides = {
                    Cpu    = "2048"
                    Memory = "8192"
                    ContainerOverrides = [
                      {
                        Name = "crosssection-data"
                        Environment = [
                          { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                          { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                          { Name = "CROSSSECTION_JOB_NAME", Value = "RunUSASpending" },
                          { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/USASpending.py" },
                          { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/a_aCompustat.parquet\",\"pyData/Intermediate/USASpending.parquet\",\"pyData/Intermediate/USASpending_uei_cache.parquet\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/a_aCompustat.parquet\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/USASpending.parquet\",\"pyData/Intermediate/USASpending_uei_cache.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/USASpending.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                End            = true
              }
            }
          }
        ]
        ResultPath = "$.data_ingress_linking_result"
        Next       = "RunSignalMasterTable"
      }
      # Phase 3: Signal master table (needs CRSP + Compustat + IBES-CRSP link)
      RunSignalMasterTable = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.data_ingress_downloads_task_family
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
                Name = "crosssection-data"
                Environment = [
                  { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                  { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                  { Name = "CROSSSECTION_JOB_NAME", Value = "RunSignalMasterTable" },
                  { Name = "CROSSSECTION_SCRIPT", Value = "SignalMasterTable.py" },
                  { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                  { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]" },
                  { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\",\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]" },
                  { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\"]" },
                  { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\"]" },
                ]
              }
            ]
          }
        }
        ResultSelector = { statusCode = 200 }
        Retry          = local.sfn_retry_ecs
        ResultPath     = "$.signal_master_table_result"
        Next           = "RunCCMLinkingTable"
      }
      # Phase 4: CCM linking table (needs SignalMasterTable + Compustat + ticker map)
      RunCCMLinkingTable = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.data_ingress_downloads_task_family
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
                Name = "crosssection-data"
                Environment = [
                  { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                  { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                  { Name = "CROSSSECTION_JOB_NAME", Value = "RunCCMLinkingTable" },
                  { Name = "CROSSSECTION_SCRIPT", Value = "DataDownloads/CCMLinkingTable.py" },
                  { Name = "CROSSSECTION_SCRIPT_ARGS", Value = "[]" },
                  { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\",\"pyData/Intermediate/ticker_to_permno.csv\"]" },
                  { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\"]" },
                  { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/CCMLinkingTable.parquet\"]" },
                  { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/CCMLinkingTable.parquet\"]" },
                ]
              }
            ]
          }
        }
        ResultSelector = { statusCode = 200 }
        Retry          = local.sfn_retry_ecs
        ResultPath     = "$.ccm_linking_table_result"
        Next           = "RunDataIngressPredictors"
      }
      RunDataIngressPredictors = {
        Type     = "Task"
        Resource = "arn:aws:states:::ecs:runTask.sync"
        Parameters = {
          LaunchType     = "FARGATE"
          Cluster        = aws_ecs_cluster.main.arn
          TaskDefinition = local.data_ingress_predictors_task_family
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
                Name = "crosssection-predictors"
                Environment = [
                  {
                    Name      = "EXECUTION_ID"
                    "Value.$" = "$$.Execution.Id"
                  },
                  {
                    Name      = "STEP_FUNCTION_STATE_NAME"
                    "Value.$" = "$$.State.Name"
                  },
                  {
                    Name  = "CROSSSECTION_JOB_NAME"
                    Value = "RunDataIngressPredictors"
                  },
                  {
                    Name  = "CROSSSECTION_REQUIRED_INPUTS"
                    Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/dailyCRSP.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\",\"pyData/Intermediate/a_aCompustat.parquet\",\"pyData/Intermediate/m_aCompustat.parquet\",\"pyData/Intermediate/m_QCompustat.parquet\",\"pyData/Intermediate/monthlyFF.parquet\",\"pyData/Intermediate/dailyFF.parquet\",\"pyData/Intermediate/monthlyMarket.parquet\",\"pyData/Intermediate/TR_13F.parquet\",\"pyData/Intermediate/IBES_EPS_Adj.parquet\",\"pyData/Intermediate/IBES_EPS_Unadj.parquet\",\"pyData/Intermediate/IBES_UnadjustedActuals.parquet\",\"pyData/Intermediate/IBES_Recommendations.parquet\",\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]"
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
        Retry      = local.sfn_retry_ecs
        ResultPath = "$.data_ingress_result"
        Next       = "RunAlphaModel"
      }
      RunAlphaModel = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.alpha_model_lambda_arn
          Payload = {
            s3_prefix               = "alpha-model"
            "universe_path.$"       = "$.universe_result.s3_output_path"
            "data_ingress_prefix.$" = "$.data_ingress_result.s3_output_prefix"
            output_key              = "alpha-model/expected_returns.csv"
            "execution_id.$"        = "$$.Execution.Id"
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
              {
                Variable      = "$.alpha_model_result.statusCode"
                NumericEquals = 200
              },
              {
                Variable      = "$.alpha_model_result.used_fallback"
                BooleanEquals = false
              },
              {
                Variable           = "$.alpha_model_result.row_count"
                NumericGreaterThan = 0
              }
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
                  { Name = "S3_UNIVERSE_KEY", Value = "data-ingress/Static/universe.csv" },
                  { Name = "S3_CRSP_PARQUET_KEY", Value = "data-ingress/pyData/Intermediate/dailyCRSP.parquet" },
                  { Name = "S3_TICKER_MAP_KEY", Value = "data-ingress/pyData/Intermediate/ticker_to_permno.csv" },
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
        Next       = "RunExecutionPhase1"
      }
      # Phase 1: submit sell-side orders as MOO (after EOD)
      RunExecutionPhase1 = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.execution_model_lambda_arn
          Payload = {
            "portfolio_weights_path.$" = "$.portfolio_result.s3_output_path"
            output_key                 = "execution-model/execution_report_close.json"
            side                       = "close"
            dry_run                    = false
            "execution_id.$"           = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"       = "$.Payload.statusCode"
          "execution_status.$" = "$.Payload.execution_status"
          "next_market_open.$" = "$.Payload.next_market_open"
        }
        Retry      = local.sfn_retry_lambda
        ResultPath = "$.phase1_result"
        Next       = "CheckPhase1Result"
      }
      CheckPhase1Result = {
        Type = "Choice"
        Choices = [
          {
            And = [
              {
                Variable      = "$.phase1_result.statusCode"
                NumericEquals = 200
              },
              {
                Variable     = "$.phase1_result.execution_status"
                StringEquals = "ok"
              }
            ]
            Next = "WaitForMarketOpen"
          }
        ]
        Default = "ExecutionPhase1Failed"
      }
      # Wait until next market open (timestamp returned by phase 1 from Alpaca clock)
      WaitForMarketOpen = {
        Type          = "Wait"
        TimestampPath = "$.phase1_result.next_market_open"
        Next          = "RunExecutionPhase2"
      }
      # Phase 2: submit buy-side orders as day orders (at market open)
      RunExecutionPhase2 = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.execution_model_lambda_arn
          Payload = {
            "portfolio_weights_path.$" = "$.portfolio_result.s3_output_path"
            output_key                 = "execution-model/execution_report_open.json"
            side                       = "open"
            dry_run                    = false
            "execution_id.$"           = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"       = "$.Payload.statusCode"
          "execution_status.$" = "$.Payload.execution_status"
        }
        Retry      = local.sfn_retry_lambda
        ResultPath = "$.phase2_result"
        Next       = "CheckExecutionModelResult"
      }
      CheckExecutionModelResult = {
        Type = "Choice"
        Choices = [
          {
            And = [
              {
                Variable      = "$.phase2_result.statusCode"
                NumericEquals = 200
              },
              {
                Variable     = "$.phase2_result.execution_status"
                StringEquals = "ok"
              }
            ]
            Next = "ExecutionSucceeded"
          }
        ]
        Default = "ExecutionModelFailed"
      }
      ExecutionSucceeded = {
        Type = "Succeed"
      }
      ExecutionPhase1Failed = {
        Type  = "Fail"
        Error = "ExecutionPhase1Failed"
        Cause = "Execution model phase 1 (close/sell) reported an error."
      }
      ExecutionModelFailed = {
        Type  = "Fail"
        Error = "ExecutionModelFailed"
        Cause = "Execution model phase 2 (open/buy) reported an error."
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
