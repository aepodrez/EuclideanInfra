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
    assert "$$.Map.Item.Value.function_name" not in DEFINITION.read_text()
    assert invoke["Catch"] == [{
        "ErrorEquals": ["States.ALL"],
        "Next": "RecordPredictorFailure",
        "ResultPath": "$.failure",
    }]
    assert processor["RecordPredictorFailure"]["Type"] == "Pass"


def test_availability_reconciliation_gates_alpha_on_potent_catalog():
    states = _definition()["States"]
    availability = states["BuildPredictorAvailability"]

    assert availability["Parameters"]["Payload"] == {"mode": "reconcile"}
    assert availability["Next"] == "CheckPredictorAvailability"
    gate = states["CheckPredictorAvailability"]
    variables = {
        condition["Variable"]
        for condition in gate["Choices"][0]["And"]
    }
    assert variables == {
        "$.predictor_availability_result.Payload.states.failed_quality",
        "$.predictor_availability_result.Payload.states.active",
        "$.predictor_availability_result.Payload.row_count",
    }
    assert gate["Default"] == "PredictorAvailabilityFailed"
