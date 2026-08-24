import json
from pathlib import Path


DEFINITION = Path(__file__).parents[1] / "infra" / "assets" / "pipeline_definition.json"


def _definition():
    return json.loads(DEFINITION.read_text())


def test_predictor_map_uses_item_payload_path_and_collects_branch_failures():
    definition = _definition()
    processor = definition["States"]["RunPredictors"]["ItemProcessor"]["States"]
    invoke = processor["InvokePredictor"]

    assert invoke["Parameters"]["FunctionName.$"] == "$.function_name"
    item_selector = definition["States"]["RunPredictors"]["ItemSelector"]
    assert item_selector == {
        "as_of_month.$": "$.monthly_context.as_of_month",
        "function_name.$": "$$.Map.Item.Value.function_name",
        "preflight.$": "$.monthly_context.preflight",
        "predictor.$": "$$.Map.Item.Value.predictor",
        "quality_migration.$": "$.monthly_context.quality_migration",
        "run_id.$": "$.monthly_context.run_id",
        "signal_master_key.$": "$.signal_master_result.Payload.signal_master_key",
        "signal_master_sha256.$": "$.signal_master_result.Payload.signal_master_sha256",
    }
    assert invoke["Parameters"]["Payload"] == {
        "as_of_month.$": "$.as_of_month",
        "preflight.$": "$.preflight",
        "quality_migration.$": "$.quality_migration",
        "run_id.$": "$.run_id",
        "signal_master_key.$": "$.signal_master_key",
        "signal_master_sha256.$": "$.signal_master_sha256",
    }
    assert invoke["Catch"] == [{
        "ErrorEquals": ["States.ALL"],
        "Next": "RecordPredictorFailure",
        "ResultPath": "$.failure",
    }]
    assert processor["RecordPredictorFailure"]["Type"] == "Pass"


def test_availability_reconciliation_gates_alpha_on_potent_catalog():
    states = _definition()["States"]
    availability = states["BuildPredictorAvailability"]

    assert availability["Parameters"]["Payload"] == {
        "as_of_month.$": "$.monthly_context.as_of_month",
        "mode": "reconcile",
        "preflight.$": "$.monthly_context.preflight",
        "run_id.$": "$.monthly_context.run_id",
        "source_snapshot_sha256.$": "$.monthly_context.source_snapshot_sha256",
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
    assert states["RunSignalMaster"]["Next"] == "CheckSignalMaster"
    assert states["RunSignalMaster"]["Parameters"]["Payload"]["preflight.$"] == (
        "$.monthly_context.preflight"
    )
    assert states["CheckSignalMaster"]["Choices"][0]["Next"] == "PreparePredictors"
    assert states["CheckSignalMaster"]["Default"] == "SignalMasterFailed"


def test_preflight_skips_portfolio_and_production_defaults_fail_closed():
    states = _definition()["States"]
    assert states["CheckAlphaModelResult"]["Choices"][0]["Next"] == "CheckPortfolioPublication"
    gate = states["CheckPortfolioPublication"]
    assert gate["Choices"][0] == {
        "BooleanEquals": True,
        "Next": "PreflightSucceeded",
        "Variable": "$.monthly_context.preflight",
    }
    assert gate["Default"] == "MonthlyProductionDisabled"


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
