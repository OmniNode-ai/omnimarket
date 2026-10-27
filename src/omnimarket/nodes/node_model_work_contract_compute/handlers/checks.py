"""Pure verdict computation. Commands belong to the existing lab runner."""

import json

from jsonschema import Draft202012Validator

from ..models.model_check import ModelCheckRequest, ModelCheckVerdict
from ..models.model_work_contract import SchemaSpec


def evaluate_schema(spec: SchemaSpec, answer: str) -> bool:
    try:
        value = json.loads(answer)
        validator = Draft202012Validator(json.loads(spec.schema_document))
        if not validator.is_valid(value):
            return False
        return spec.expected_json is None or value == json.loads(spec.expected_json)
    except (ValueError, TypeError):
        return False


class HandlerModelWorkContract:
    """Compare a recorded check result with its pinned expectation, without I/O."""

    def handle(self, request: ModelCheckRequest) -> ModelCheckVerdict:
        contract, result = request.contract, request.result
        expected = (
            0
            if contract.acceptance.kind == "schema"
            else contract.acceptance.spec.expected_exit
        )
        passed = (
            result.contract_digest == contract.digest()
            and result.evaluator_intact
            and not result.timed_out
            and result.tests_collected > 0
            and result.exit_code == expected
        )
        return ModelCheckVerdict(
            contract_id=contract.contract_id,
            passed=passed,
            reason="accepted" if passed else "check_failed",
        )
