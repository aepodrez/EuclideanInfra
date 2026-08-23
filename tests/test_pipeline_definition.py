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
        "predictor.$": "$$.Map.Item.Value.predictor",
        "run_id.$": "$.monthly_context.run_id",
    }
    assert invoke["Parameters"]["Payload"] == {
        "as_of_month.$": "$.as_of_month",
        "predictor.$": "$.predictor",
        "run_id.$": "$.run_id",
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
        "run_id.$": "$.monthly_context.run_id",
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


def test_monthly_context_is_resolved_once_and_pins_alpha_catalog():
    definition = _definition()
    states = definition["States"]

    assert definition["StartAt"] == "ResolveMonthlyContext"
    assert states["ResolveMonthlyContext"]["Parameters"]["Payload"] == {
        "execution_id.$": "$$.Execution.Id",
        "mode": "context",
        "request.$": "$",
    }
    alpha = states["RunAlphaModel"]["Parameters"]["Payload"]
    assert alpha["as_of_month.$"] == "$.monthly_context.as_of_month"
    assert alpha["predictor_availability_key.$"] == (
        "$.predictor_availability_result.Payload.output"
    )
    assert alpha["predictor_availability_sha256.$"] == (
        "$.predictor_availability_result.Payload.output_sha256"
    )
    assert alpha["output_key.$"].startswith("States.Format('alpha-model/runs/")


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


def test_portfolio_consumes_exact_alpha_and_catalog_lineage():
    portfolio = _definition()["States"]["RunPortfolioConstruction"]
    environment = {
        item["Name"]: item.get("Value.$", item.get("Value"))
        for item in portfolio["Parameters"]["Overrides"]["ContainerOverrides"][0]["Environment"]
    }

    assert environment["S3_EXPECTED_RETURNS_KEY"] == "$.alpha_model_result.s3_output_path"
    assert environment["S3_EXPECTED_RETURNS_SHA256"] == "$.alpha_model_result.output_sha256"
    assert environment["MONTHLY_RUN_ID"] == "$.monthly_context.run_id"
    assert environment["PREDICTOR_AVAILABILITY_KEY"] == (
        "$.predictor_availability_result.Payload.output"
    )
    assert environment["PREDICTOR_AVAILABILITY_SHA256"] == (
        "$.predictor_availability_result.Payload.output_sha256"
    )
    assert "portfolio-construction/runs/{}/" in environment["S3_OUTPUT_KEY"]
