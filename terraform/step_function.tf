# Locals for constructing ARNs based on naming conventions
locals {
  universe_task_family                         = "${var.project_name}-universe-${var.environment}"
  alpha_model_lambda_arn                       = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-alpha-model-${var.environment}"
  portfolio_construction_lambda_arn            = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-portfolio-construction-${var.environment}"
  execution_model_lambda_arn                   = "arn:aws:lambda:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:function:${var.project_name}-execution-model-${var.environment}"
  data_ingress_downloads_task_family           = "${var.project_name}-data-ingress-downloads-${var.environment}"
  data_ingress_compustat_annual_task_family    = "${var.project_name}-data-ingress-compustat-annual-${var.environment}"
  data_ingress_compustat_quarterly_task_family = "${var.project_name}-data-ingress-compustat-quarterly-${var.environment}"
  data_ingress_refinitiv_task_family           = "${var.project_name}-data-ingress-refinitiv-${var.environment}"
  data_ingress_predictors_task_family          = "${var.project_name}-data-ingress-predictors-${var.environment}"
  sfn_retry_ecs = [
    {
      ErrorEquals     = ["States.ALL"]
      IntervalSeconds = 20
      MaxAttempts     = 3
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

  bulk_data_jobs = [
    {
      job_name         = "RunCRSPDaily"
      script           = "DataDownloads/CRSPDaily.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode(["Static/universe.csv", "pyData/Intermediate/dailyCRSP.parquet", "pyData/Intermediate/dailyCRSPprc.parquet", "pyData/Intermediate/cache/crsp_daily/"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/dailyCRSP.parquet", "pyData/Intermediate/dailyCRSPprc.parquet", "pyData/Intermediate/cache/crsp_daily/"])
      expected_outputs = jsonencode(["pyData/Intermediate/dailyCRSP.parquet", "pyData/Intermediate/dailyCRSPprc.parquet"])
    },
    {
      job_name         = "RunCRSPDistributions"
      script           = "DataDownloads/CRSPDistributions.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/CRSPdistributions.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/CRSPdistributions.parquet"])
    },
    {
      job_name         = "RunBEAInputOutput"
      script           = "DataDownloads/BEAInputOutput.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode([])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/BEA_Supply_Table.parquet", "pyData/Intermediate/BEA_Supply_Table.csv", "pyData/Intermediate/BEA_SupplyUse_Framework.parquet", "pyData/Intermediate/BEA_SupplyUse_Framework.csv"])
      expected_outputs = jsonencode(["pyData/Intermediate/BEA_Supply_Table.parquet", "pyData/Intermediate/BEA_SupplyUse_Framework.parquet"])
    },
    {
      job_name         = "RunCompustatAnnual"
      script           = "DataDownloads/CompustatAnnual.py"
      script_args      = jsonencode(["--universe_csv", "../Static/universe.csv", "--output_dir", "../pyData/Intermediate/compustat_annual"])
      task_definition  = local.data_ingress_compustat_annual_task_family
      task_cpu         = "2048"
      task_memory      = "10240"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode(["Static/universe.csv"])
      output_allowlist = jsonencode(["pyData/Intermediate/compustat_annual/outputs/features.parquet", "pyData/Intermediate/compustat_annual/outputs/features.csv", "pyData/Intermediate/compustat_annual/outputs/diagnostics_anchor_residuals.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/compustat_annual/outputs/features.parquet"])
    },
    {
      job_name         = "RunCompustatQuarterly"
      script           = "DataDownloads/CompustatQuarterly.py"
      script_args      = jsonencode(["--universe_csv", "../Static/universe.csv", "--output_dir", "../pyData/Intermediate/compustat_quarterly"])
      task_definition  = local.data_ingress_compustat_quarterly_task_family
      task_cpu         = "2048"
      task_memory      = "10240"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode(["Static/universe.csv"])
      output_allowlist = jsonencode(["pyData/Intermediate/compustat_quarterly/outputs/quarterly_features.parquet", "pyData/Intermediate/compustat_quarterly/outputs/quarterly_features.csv", "pyData/Intermediate/compustat_quarterly/outputs/quarterly_diagnostics_anchor_residuals.parquet", "pyData/Intermediate/compustat_quarterly/outputs/m_QCompustatV2.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/compustat_quarterly/outputs/quarterly_features.parquet"])
    },
    {
      job_name         = "RunCompustatShortInterest"
      script           = "DataDownloads/CompustatShortInterest.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode([])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/monthlyShortInterest.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/monthlyShortInterest.parquet"])
    },
    {
      job_name         = "RunGNPDeflator"
      script           = "DataDownloads/GNPDeflator.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode([])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/GNPdefl.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/GNPdefl.parquet"])
    },
    {
      job_name         = "RunInstitutionalHoldings13F"
      script           = "DataDownloads/InstitutionalHoldings13F.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/TR_13F.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/TR_13F.parquet"])
    },
    {
      job_name         = "RunMarketReturns"
      script           = "DataDownloads/MarketReturns.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/monthlyMarket.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/monthlyMarket.parquet"])
    },
    {
      job_name         = "RunQFactorModel"
      script           = "DataDownloads/QFactorModel.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode(["Static/universe.csv"])
      output_allowlist = jsonencode(["pyData/Intermediate/d_qfactor_live.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/d_qfactor_live.parquet"])
    },
    {
      job_name         = "RunTreasuryBill3M"
      script           = "DataDownloads/TreasuryBill3M.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode([])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/TBill3M.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/TBill3M.parquet"])
    },
    {
      job_name         = "RunVIX"
      script           = "DataDownloads/VIX.py"
      script_args      = jsonencode([])
      task_definition  = local.data_ingress_downloads_task_family
      task_cpu         = "1024"
      task_memory      = "4096"
      input_allowlist  = jsonencode([])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/d_vix.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/d_vix.parquet"])
    },
  ]

  bulk_refinitiv_jobs = [
    {
      job_name         = "RunIBESEPSUnadjusted"
      script           = "DataDownloads/IBESEPSUnadjusted.py"
      script_args      = jsonencode([])
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/IBES_EPS_Unadj.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/IBES_EPS_Unadj.parquet"])
    },
    {
      job_name         = "RunIBESUnadjustedActuals"
      script           = "DataDownloads/IBESUnadjustedActuals.py"
      script_args      = jsonencode([])
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/IBES_UnadjustedActuals.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/IBES_UnadjustedActuals.parquet"])
    },
    {
      job_name         = "RunIBESRecommendations"
      script           = "DataDownloads/IBESRecommendations.py"
      script_args      = jsonencode([])
      input_allowlist  = jsonencode(["Static/universe.csv"])
      required_inputs  = jsonencode([])
      output_allowlist = jsonencode(["pyData/Intermediate/IBES_Recommendations.parquet"])
      expected_outputs = jsonencode(["pyData/Intermediate/IBES_Recommendations.parquet"])
    },
  ]
}

# Step Functions State Machine
resource "aws_sfn_state_machine" "pipeline" {
  name     = "${var.project_name}-pipeline-${var.environment}"
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
          {
            StartAt = "SeedCoreParallel"
            States = {
              SeedCoreParallel = {
                Type = "Parallel"
                Branches = [
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
                        End            = true
                      }
                    }
                  },
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
                                  { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"Static/universe.csv\"]" },
                                  { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[]" },
                                  { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/IBES_EPS_Adj.parquet\"]" },
                                  { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/IBES_EPS_Adj.parquet\"]" },
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
                Next = "PostSeedDependentParallel"
              }
              PostSeedDependentParallel = {
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
                Next = "RunIBESCRSPLink"
              }
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
                Next           = "RunSignalMasterTable"
              }
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
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\",\"pyData/Intermediate/IBESCRSPLinkingTable.parquet\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/monthlyCRSP.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\"]" },
                        ]
                      }
                    ]
                  }
                }
                ResultSelector = { statusCode = 200 }
                Retry          = local.sfn_retry_ecs
                Next           = "RunCCMLinkingTable"
              }
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
                          { Name = "CROSSSECTION_INPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\",\"pyData/Intermediate/ticker_to_permno.csv\",\"pyData/Intermediate/ticker_to_permno_monthly.csv\"]" },
                          { Name = "CROSSSECTION_REQUIRED_INPUTS", Value = "[\"pyData/Intermediate/SignalMasterTable.parquet\"]" },
                          { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", Value = "[\"pyData/Intermediate/CCMLinkingTable.parquet\"]" },
                          { Name = "CROSSSECTION_EXPECTED_OUTPUTS", Value = "[\"pyData/Intermediate/CCMLinkingTable.parquet\"]" },
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
            StartAt = "PrepareBulkDataJobs"
            States = {
              PrepareBulkDataJobs = {
                Type       = "Pass"
                Result     = local.bulk_data_jobs
                ResultPath = "$.jobs"
                Next       = "RunBulkDataJobs"
              }
              RunBulkDataJobs = {
                Type           = "Map"
                ItemsPath      = "$.jobs"
                MaxConcurrency = 10
                Iterator = {
                  StartAt = "RunBulkDataJob"
                  States = {
                    RunBulkDataJob = {
                      Type     = "Task"
                      Resource = "arn:aws:states:::ecs:runTask.sync"
                      Parameters = {
                        LaunchType         = "FARGATE"
                        Cluster            = aws_ecs_cluster.main.arn
                        "TaskDefinition.$" = "$.task_definition"
                        NetworkConfiguration = {
                          AwsvpcConfiguration = {
                            Subnets        = [for subnet in aws_subnet.public : subnet.id]
                            SecurityGroups = [aws_security_group.ecs.id]
                            AssignPublicIp = "ENABLED"
                          }
                        }
                        Overrides = {
                          "Cpu.$"    = "$.task_cpu"
                          "Memory.$" = "$.task_memory"
                          ContainerOverrides = [
                            {
                              Name = "crosssection-data"
                              Environment = [
                                { Name = "EXECUTION_ID", "Value.$" = "$$.Execution.Id" },
                                { Name = "STEP_FUNCTION_STATE_NAME", "Value.$" = "$$.State.Name" },
                                { Name = "CROSSSECTION_JOB_NAME", "Value.$" = "$.job_name" },
                                { Name = "CROSSSECTION_SCRIPT", "Value.$" = "$.script" },
                                { Name = "CROSSSECTION_SCRIPT_ARGS", "Value.$" = "$.script_args" },
                                { Name = "CROSSSECTION_INPUT_ALLOWLIST", "Value.$" = "$.input_allowlist" },
                                { Name = "CROSSSECTION_REQUIRED_INPUTS", "Value.$" = "$.required_inputs" },
                                { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", "Value.$" = "$.output_allowlist" },
                                { Name = "CROSSSECTION_EXPECTED_OUTPUTS", "Value.$" = "$.expected_outputs" },
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
                End = true
              }
            }
          },
          {
            StartAt = "PrepareBulkRefinitivJobs"
            States = {
              PrepareBulkRefinitivJobs = {
                Type       = "Pass"
                Result     = local.bulk_refinitiv_jobs
                ResultPath = "$.jobs"
                Next       = "RunBulkRefinitivJobs"
              }
              RunBulkRefinitivJobs = {
                Type           = "Map"
                ItemsPath      = "$.jobs"
                MaxConcurrency = 4
                Iterator = {
                  StartAt = "RunBulkRefinitivJob"
                  States = {
                    RunBulkRefinitivJob = {
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
                                { Name = "CROSSSECTION_JOB_NAME", "Value.$" = "$.job_name" },
                                { Name = "CROSSSECTION_REFINITIV_SCRIPTS", "Value.$" = "$.script" },
                                { Name = "CROSSSECTION_SCRIPT_ARGS", "Value.$" = "$.script_args" },
                                { Name = "CROSSSECTION_INPUT_ALLOWLIST", "Value.$" = "$.input_allowlist" },
                                { Name = "CROSSSECTION_REQUIRED_INPUTS", "Value.$" = "$.required_inputs" },
                                { Name = "CROSSSECTION_OUTPUT_ALLOWLIST", "Value.$" = "$.output_allowlist" },
                                { Name = "CROSSSECTION_EXPECTED_OUTPUTS", "Value.$" = "$.expected_outputs" },
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
                End = true
              }
            }
          },
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
        Next       = "RunDataIngressPredictors"
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
        }
        Retry      = local.sfn_retry_lambda
        ResultPath = "$.alpha_model_result"
        Next       = "RunPortfolioConstruction"
      }
      RunPortfolioConstruction = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = local.portfolio_construction_lambda_arn
          Payload = {
            s3_prefix                 = "portfolio-construction"
            "expected_returns_path.$" = "$.alpha_model_result.s3_output_path"
            universe_path             = "data-ingress/Static/universe.csv"
            daily_crsp_path           = "data-ingress/pyData/Intermediate/dailyCRSP.parquet"
            ticker_map_path           = "data-ingress/pyData/Intermediate/ticker_to_permno.csv"
            output_key                = "portfolio-construction/optimal_portfolio.csv"
            meta_key                  = "portfolio-construction/optimal_portfolio_meta.json"
            "execution_id.$"          = "$$.Execution.Id"
          }
        }
        ResultSelector = {
          "statusCode.$"     = "$.Payload.statusCode"
          "s3_output_path.$" = "$.Payload.s3_output_path"
        }
        Retry      = local.sfn_retry_lambda
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
        Retry      = local.sfn_retry_lambda
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
