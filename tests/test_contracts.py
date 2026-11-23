"""Independent expected-output vectors (not derived from implementation logic)."""
import json
from pathlib import Path
import pytest

from verified_execution.core import CalculationService, LocalAuthority

ROOT = Path(__file__).resolve().parents[1]
VECTORS = [json.loads(line) for line in (ROOT / "contracts" / "expected_cases.jsonl").read_text().splitlines()]

@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["case"])
def test_expected_case(vector):
    result = CalculationService(LocalAuthority.calculator_only()).execute(vector["name"], vector["arguments"])
    actual = result.to_dict()
    assert {key: actual[key] for key in vector["expected"]} == vector["expected"]


def test_json_schema_contract():
    import jsonschema
    schema = json.loads((ROOT / "contracts" / "checked_add.input.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    for vector in VECTORS:
        if vector["name"] != "checked_add":
            continue
        input_valid = not list(jsonschema.Draft202012Validator(schema).iter_errors(vector["arguments"]))
        # JSON Schema calls 1.0 an integer by mathematical value. Runtime uses
        # a stricter lexical Python int boundary; that difference is explicit.
        if vector["case"] == "CALC-006":
            assert input_valid, "JSON Schema defines 1.0 as an integer value"
            continue
        # Overflow is a semantic operation failure, not an input-schema error.
        expected_valid = vector["expected"]["error_code"] in (None, "OVERFLOW")
        assert input_valid == expected_valid, vector["case"]
