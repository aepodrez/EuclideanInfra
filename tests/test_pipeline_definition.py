import json
from pathlib import Path


DEFINITION = Path(__file__).parents[1] / "infra" / "assets" / "pipeline_definition.json"
INFRA_STACK = (
    Path(__file__).parents[1] / "infra" / "stacks" / "euclidean_infra_stack.py"
)


def _definition():
    return json.loads(DEFINITION.read_text())


def test_ibes_event_role_can_pass_the_scoped_refinitiv_runtime_role():
    source = INFRA_STACK.read_text()

    assert 'role_arn("euclidean-data-ingress-task-role-refinitiv-runtime")' in source


def test_predictor_map_uses_item_payload_path_and_collects_branch_failures():
    definition = _definition()
    processor = definition["States"]["RunPredictors"]["ItemProcessor"]["States"]
    invoke = processor["InvokePredictor"]

    assert invoke["Parameters"]["FunctionName"] == (
        "arn:aws:lambda:us-east-1:954976294836:function:euclidean-pred-worker"
    )
    item_selector = definition["States"]["RunPredictors"]["ItemSelector"]
    assert item_selector == {
        "as_of_month.$": "$.monthly_context.as_of_month",
        "preflight.$": "$.monthly_context.preflight",
        "predictor.$": "$$.Map.Item.Value.predictor",
        "quality_migration.$": "$.monthly_context.quality_migration",
        "run_id.$": "$.monthly_context.run_id",
        "signal_master_key.$": "$.signal_master_result.Payload.signal_master_key",
        "signal_master_sha256.$": "$.signal_master_result.Payload.signal_master_sha256",
        "source_snapshot_key.$": "$.monthly_context.source_snapshot_key",
        "source_snapshot_sha256.$": "$.monthly_context.source_snapshot_sha256",
    }
    assert invoke["Parameters"]["Payload"] == {
        "as_of_month.$": "$.as_of_month",
        "preflight.$": "$.preflight",
        "predictor.$": "$.predictor",
        "quality_migration.$": "$.quality_migration",
        "run_id.$": "$.run_id",
        "signal_master_key.$": "$.signal_master_key",
        "signal_master_sha256.$": "$.signal_master_sha256",
        "source_snapshot_key.$": "$.source_snapshot_key",
        "source_snapshot_sha256.$": "$.source_snapshot_sha256",
    }
    assert invoke["Catch"] == [{
        "ErrorEquals": ["States.ALL"],
        "Next": "RecordPredictorFailure",
        "ResultPath": "$.failure",
    }]
    assert processor["RecordPredictorFailure"]["Type"] == "Pass"
    assert definition["States"]["RunPredictors"]["Next"] == "InitializeRIVolStatus"
    assert definition["States"]["RunPredictors"]["ItemsPath"] == (
        "$.predictor_dispatch.Payload.items"
    )
    assert definition["States"]["RunRIVolSpread"]["Parameters"]["Payload"][
        "source_snapshot_key.$"
    ] == "$.monthly_context.source_snapshot_key"
    assert definition["States"]["MarkRIVolFailed"]["Result"] == {
        "status": "failed",
        "predictor": "ZZ1_RIVolSpread",
    }


def test_high_memory_predictors_use_bounded_on_demand_task():
    processor = _definition()["States"]["RunPredictors"]["ItemProcessor"]
    states = processor["States"]
    assert processor["StartAt"] == "ChoosePredictorRuntime"
    choice = states["ChoosePredictorRuntime"]
    assert choice["Default"] == "InvokePredictor"
    high_memory_predictors = [part["StringEquals"] for part in choice["Choices"][0]["Or"]]
    assert len(high_memory_predictors) == len(set(high_memory_predictors))
    assert set(high_memory_predictors) == {
        "CBOperProf", "EntMult", "EquityDuration", "ExchSwitch",
        "ChAssetTurnover", "Coskewness", "CustomerMomentum", "DelCOL",
        "DelDRC", "DivYieldST", "Frontier", "IndMom",
        "GrLTNOA", "GrSaleToGrInv", "HerfAsset", "Herf",
        "GrSaleToGrOverhead", "HerfBE", "Investment", "InvestPPEInv",
        "MomOffSeason", "MS", "MomVol", "OperProfRD", "OPLeverage",
        "OrderBacklog", "OrderBacklogChg", "PctAcc", "PctTotAcc",
        "OScore", "PS", "RDIPO", "realestate", "sfe", "sinAlgo",
        "SurpriseRD", "TotalAccruals", "VarCF", "XFIN",
        "ZZ1_AnalystValue_AOP_PredictedFE_IntrinsicValue", "ZZ1_EBM_BPEBM",
        "ZZ1_grcapx_grcapx1y_grcapx3y",
        "ZZ1_RIO_MB_RIO_Disp_RIO_Turnover_RIO_Volatility",
    }
    assert set(high_memory_predictors) <= {
        item["predictor"] for item in _definition()["States"]["PreparePredictors"]["Result"]
    }
    task = states["RunHighMemoryPredictor"]
    assert task["Resource"] == "arn:aws:states:::ecs:runTask.sync"
    assert task["Parameters"]["TaskDefinition"] == "euclidean-monthly-predictor-high-memory"
    assert task["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"] == [
        {"Name": "PREDICTOR_EVENT_JSON", "Value.$": "States.JsonToString($)"}
    ]
    assert task["TimeoutSeconds"] == 900
    assert task["Catch"][0]["Next"] == "RecordPredictorFailure"
    assert states["CheckHighMemoryPredictorExit"]["Default"] == "RecordHighMemoryPredictorFailure"
    assert states["InvokePredictor"]["Retry"][0]["ErrorEquals"] == [
        "Lambda.ServiceException", "Lambda.AWSLambdaException",
        "Lambda.SdkClientException", "Lambda.TooManyRequestsException",
    ]
    assert "task-definition/euclidean-monthly-predictor-high-memory:*" in INFRA_STACK.read_text()


def test_monthly_start_waits_for_exact_source_readiness_and_seals_dispatch():
    states = _definition()["States"]
    context = states["ResolveMonthlyContext"]
    assert context["Next"] == "CheckMonthlySourceReadiness"
    choice = states["CheckMonthlySourceReadiness"]["Choices"]
    assert choice == [
        {"Variable": "$.monthly_context.statusCode", "NumericEquals": 200,
         "Next": "RunSignalMaster"},
        {"Variable": "$.monthly_context.statusCode", "NumericEquals": 202,
         "Next": "WaitForMonthlySources"},
    ]
    assert states["WaitForMonthlySources"] == {
        "Type": "Wait", "Seconds": 900, "Next": "ResolveMonthlyContext",
    }
    assert states["PreparePredictors"]["Next"] == "FilterCompletedPredictors"
    dispatch = states["FilterCompletedPredictors"]
    assert dispatch["Parameters"]["Payload"]["expected.$"] == "$.predictors"
    assert dispatch["Parameters"]["Payload"]["mode"] == "dispatch"
    assert dispatch["Next"] == "RunPredictors"


def test_availability_reconciliation_gates_alpha_on_potent_catalog():
    states = _definition()["States"]
    availability = states["BuildPredictorAvailability"]

    assert availability["Parameters"]["Payload"] == {
        "as_of_month.$": "$.monthly_context.as_of_month",
        "dispatch_index_key.$": "$.predictor_dispatch.Payload.dispatch_index_key",
        "dispatch_index_sha256.$": "$.predictor_dispatch.Payload.dispatch_index_sha256",
        "mode": "reconcile",
        "preflight.$": "$.monthly_context.preflight",
        "predictor_results.$": "$.predictor_results",
        "rivol_status.$": "$.rivol_status",
        "run_id.$": "$.monthly_context.run_id",
        "source_snapshot_key.$": "$.monthly_context.source_snapshot_key",
        "source_snapshot_sha256.$": "$.monthly_context.source_snapshot_sha256",
        "signal_master_sha256.$": "$.signal_master_result.Payload.signal_master_sha256",
        "signal_master_key.$": "$.signal_master_result.Payload.signal_master_key",
    }
    assert availability["Next"] == "CheckPredictorAvailability"
    gate = states["CheckPredictorAvailability"]
    variables = {
        condition["Variable"]
        for condition in gate["Choices"][0]["And"]
    }
    assert {
        "$.predictor_availability_result.Payload.states.failed_quality",
        "$.predictor_availability_result.Payload.states.active",
        "$.predictor_availability_result.Payload.row_count",
    } <= variables
    assert gate["Default"] == "PredictorAvailabilityFailed"
    numeric_equals = {
        (condition.get("Variable"), condition.get("NumericEquals"))
        for condition in gate["Choices"][0]["And"]
    }
    assert ("$.predictor_availability_result.Payload.row_count", 212) in numeric_equals


def test_monthly_context_is_resolved_once_and_pins_alpha_catalog():
    definition = _definition()
    states = definition["States"]

    assert definition["StartAt"] == "ResolveMonthlyContext"
    assert states["ResolveMonthlyContext"]["Parameters"]["Payload"] == {
        "execution_id.$": "$$.Execution.Id",
        "mode": "context",
        "request.$": "$",
    }
    assert states["ResolveMonthlyContext"]["ResultSelector"]["quality_migration.$"] == (
        "$.Payload.quality_migration"
    )
    assert states["BuildPredictorAvailability"]["Parameters"]["Payload"][
        "predictor_results.$"
    ] == "$.predictor_results"
    alpha = states["RunAlphaModel"]["Parameters"]["Payload"]
    assert alpha["as_of_month.$"] == "$.monthly_context.as_of_month"
    assert alpha["classification_registry_key.$"] == (
        "$.signal_master_result.Payload.classification_registry_key"
    )
    assert alpha["predictor_availability_key.$"] == (
        "$.predictor_availability_result.Payload.output"
    )
    assert alpha["predictor_availability_sha256.$"] == (
        "$.predictor_availability_result.Payload.output_sha256"
    )
    assert alpha["output_key.$"].startswith("States.Format('alpha-model/runs/")
    assert alpha["universe_path.$"] == "$.monthly_context.portfolio_inputs.universe.key"
    assert alpha["universe_sha256.$"] == "$.monthly_context.portfolio_inputs.universe.sha256"


def test_alpha_gate_requires_exact_month_and_catalog_hash():
    conditions = _definition()["States"]["CheckAlphaModelResult"]["Choices"][0]["And"]

    assert {
        (item.get("Variable"), item.get("NumericEqualsPath"), item.get("StringEqualsPath"))
        for item in conditions
    } >= {
        (
            "$.alpha_model_result.as_of_month",
            "$.monthly_context.as_of_month",
            None,
        ),
        (
            "$.alpha_model_result.predictor_availability_sha256",
            None,
            "$.predictor_availability_result.Payload.output_sha256",
        ),
    }


def test_signal_master_is_gated_before_predictors():
    states = _definition()["States"]
    assert states["ResolveMonthlyContext"]["ResultSelector"][
        "source_snapshot_key.$"
    ] == "$.Payload.source_snapshot_key"
    assert states["RunSignalMaster"]["Next"] == "CheckSignalMaster"
    assert states["RunSignalMaster"]["Parameters"]["Payload"]["preflight.$"] == (
        "$.monthly_context.preflight"
    )
    assert states["RunSignalMaster"]["Parameters"]["Payload"][
        "source_snapshot_sha256.$"
    ] == "$.monthly_context.source_snapshot_sha256"
    for state in ("RunSignalMaster", "BuildPredictorAvailability"):
        assert states[state]["Parameters"]["Payload"]["source_snapshot_key.$"] == (
            "$.monthly_context.source_snapshot_key"
        )
    assert states["CheckSignalMaster"]["Choices"][0]["Next"] == "PreparePredictors"
    assert states["CheckSignalMaster"]["Default"] == "SignalMasterFailed"


def test_preflight_stops_before_alpha_and_production_defaults_fail_closed():
    states = _definition()["States"]
    assert states["CheckPredictorAvailability"]["Choices"][0]["Next"] == (
        "CheckPreflightComplete"
    )
    assert states["CheckPreflightComplete"]["Choices"][0] == {
        "BooleanEquals": True,
        "Next": "PreflightComplete",
        "Variable": "$.monthly_context.preflight",
    }
    assert states["CheckPreflightComplete"]["Default"] == "RunAlphaModel"
    assert states["PreflightComplete"] == {"Type": "Succeed"}
    assert states["CheckAlphaModelResult"]["Choices"][0]["Next"] == "CheckPortfolioPublication"
    gate = states["CheckPortfolioPublication"]
    assert gate["Choices"][0] == {
        "BooleanEquals": True,
        "Next": "RunPortfolioConstruction",
        "Variable": "$.monthly_context.production_enabled",
    }
    assert gate["Default"] == "MonthlyProductionDisabled"


def test_shared_worker_receives_exact_case_sensitive_producers():
    states = _definition()["States"]
    items = states["PreparePredictors"]["Result"]

    assert len(items) == 185
    assert len({item["predictor"] for item in items}) == 185
    assert {item["function_name"] for item in items} == {
        "arn:aws:lambda:us-east-1:954976294836:function:euclidean-pred-worker"
    }
    assert "Accruals" in {item["predictor"] for item in items}
    assert "accruals" not in {item["predictor"] for item in items}
    assert states["RunRIVolSpread"]["Parameters"]["Payload"]["predictor"] == (
        "ZZ1_RIVolSpread"
    )


def test_portfolio_consumes_exact_alpha_and_catalog_lineage():
    portfolio = _definition()["States"]["RunPortfolioConstruction"]
    environment = {
        item["Name"]: item.get("Value.$", item.get("Value"))
        for item in portfolio["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"]
    }

    assert environment["S3_EXPECTED_RETURNS_KEY"] == "$.alpha_model_result.s3_output_path"
    assert environment["S3_EXPECTED_RETURNS_SHA256"] == "$.alpha_model_result.output_sha256"
    assert environment["MONTHLY_RUN_ID"] == "$.monthly_context.run_id"
    assert environment["MONTHLY_SOURCE_SNAPSHOT_SHA256"] == (
        "$.monthly_context.source_snapshot_sha256"
    )
    assert environment["S3_CRSP_PARQUET_KEY"] == (
        "$.monthly_context.portfolio_inputs.daily_crsp.key"
    )
    assert environment["S3_CRSP_PARQUET_SHA256"] == (
        "$.monthly_context.portfolio_inputs.daily_crsp.sha256"
    )
    assert environment["S3_CLASSIFICATION_REGISTRY_KEY"] == (
        "$.signal_master_result.Payload.classification_registry_key"
    )
    assert environment["PREDICTOR_AVAILABILITY_KEY"] == (
        "$.predictor_availability_result.Payload.output"
    )
    assert environment["PREDICTOR_AVAILABILITY_SHA256"] == (
        "$.predictor_availability_result.Payload.output_sha256"
    )
    assert "portfolio-construction/runs/{}/" in environment["S3_OUTPUT_KEY"]
