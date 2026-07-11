# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for node_generated_code_validator — pure code validation.

Fixtures are generated-artifact source strings. They assert: a well-formed node
is valid; each stub shape (bare ``pass`` / ``...`` / docstring-only /
``raise NotImplementedError`` / stub comment marker, sync *and* async) is
flagged; a marker appearing only inside a string literal is not a false
positive; a non-parsing artifact is rejected with the syntax error captured;
and structure mismatches (missing class / wrong base / missing method) surface
as issues. Each assertion is chosen to fail if the corresponding detection is
disabled, so the suite is mutation-discriminating rather than pass-by-shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_generated_code_validator.handlers.handler_generated_code_validator import (
    HandlerGeneratedCodeValidator,
    validate_generated_code,
)
from omnimarket.nodes.node_generated_code_validator.models.model_generated_code_validation import (
    ModelGeneratedCodeValidation,
)
from omnimarket.nodes.node_generated_code_validator.models.model_generated_code_validator_request import (
    ModelExpectedStructure,
    ModelGeneratedCodeValidatorRequest,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_generated_code_validator"
    / "contract.yaml"
)

# --- fixture artifact sources --------------------------------------------------

_WELL_FORMED_SOURCE = """\
class NodeThingCompute(NodeCompute):
    \"\"\"A real compute node.\"\"\"

    def handle(self, request):
        total = 0
        for value in request.values:
            total += value
        return total
"""

_STUB_PASS_SOURCE = """\
class NodeStubCompute(NodeCompute):
    def handle(self, request):
        pass
"""

_STUB_ELLIPSIS_SOURCE = """\
class NodeStubCompute(NodeCompute):
    def handle(self, request):
        ...
"""

_STUB_NOT_IMPLEMENTED_SOURCE = """\
class NodeStubCompute(NodeCompute):
    def handle(self, request):
        raise NotImplementedError("later")
"""

_STUB_DOCSTRING_ONLY_SOURCE = """\
class NodeStubCompute(NodeCompute):
    def handle(self, request):
        \"\"\"Does the thing.\"\"\"
"""

_STUB_TODO_COMMENT_SOURCE = """\
class NodeStubCompute(NodeCompute):
    def handle(self, request):  # TODO: implement
        return None
"""

_ASYNC_STUB_SOURCE = """\
class NodeStubEffect(NodeEffect):
    async def handle(self, request):
        ...
"""

# A method whose body only *mentions* markers inside a string literal — must not
# be flagged (no empty body, no Raise node, no comment token).
_MARKER_IN_STRING_SOURCE = """\
class NodeStringCompute(NodeCompute):
    def handle(self, request):
        return "raise NotImplementedError and TODO are only text here"
"""

_SYNTAX_ERROR_SOURCE = (
    "class Broken(NodeCompute):\n    def handle(self, request:\n        pass\n"
)


def _validate(
    source: str, expected: ModelExpectedStructure | None = None
) -> ModelGeneratedCodeValidation:
    return HandlerGeneratedCodeValidator().handle(
        ModelGeneratedCodeValidatorRequest(source_text=source, expected=expected)
    )


@pytest.mark.unit
class TestParsing:
    def test_well_formed_source_parses(self) -> None:
        result = _validate(_WELL_FORMED_SOURCE)
        assert result.parses is True
        assert result.syntax_error is None

    def test_syntax_error_is_rejected(self) -> None:
        result = _validate(_SYNTAX_ERROR_SOURCE)
        assert result.parses is False
        assert result.syntax_error is not None
        assert result.is_valid is False
        # A non-parsing artifact does not run the downstream checks.
        assert result.stub_methods == ()
        assert result.structure_issues == ()


@pytest.mark.unit
class TestStubDetection:
    @pytest.mark.parametrize(
        "source",
        [
            _STUB_PASS_SOURCE,
            _STUB_ELLIPSIS_SOURCE,
            _STUB_NOT_IMPLEMENTED_SOURCE,
            _STUB_DOCSTRING_ONLY_SOURCE,
            _STUB_TODO_COMMENT_SOURCE,
        ],
    )
    def test_sync_stub_shapes_flag_handle(self, source: str) -> None:
        result = _validate(source)
        assert result.stub_methods == ("handle",)
        assert result.is_valid is False

    def test_async_stub_is_flagged(self) -> None:
        result = _validate(_ASYNC_STUB_SOURCE)
        assert result.stub_methods == ("handle",)
        assert result.is_valid is False

    def test_well_formed_method_is_not_a_stub(self) -> None:
        result = _validate(_WELL_FORMED_SOURCE)
        assert result.stub_methods == ()

    def test_marker_only_in_string_literal_is_not_flagged(self) -> None:
        result = _validate(_MARKER_IN_STRING_SOURCE)
        assert result.stub_methods == ()
        assert result.is_valid is True


@pytest.mark.unit
class TestStructureMatch:
    def test_matching_structure_has_no_issues(self) -> None:
        expected = ModelExpectedStructure(
            class_name="NodeThingCompute",
            base_class="NodeCompute",
            required_methods=("handle",),
        )
        result = _validate(_WELL_FORMED_SOURCE, expected)
        assert result.structure_issues == ()
        assert result.is_valid is True

    def test_missing_class_is_reported(self) -> None:
        expected = ModelExpectedStructure(class_name="NodeOtherCompute")
        result = _validate(_WELL_FORMED_SOURCE, expected)
        assert result.structure_issues == (
            "expected class 'NodeOtherCompute' not found",
        )
        assert result.is_valid is False

    def test_wrong_base_class_is_reported(self) -> None:
        expected = ModelExpectedStructure(
            class_name="NodeThingCompute", base_class="NodeEffect"
        )
        result = _validate(_WELL_FORMED_SOURCE, expected)
        assert result.structure_issues == (
            "class 'NodeThingCompute' does not inherit expected base 'NodeEffect'",
        )
        assert result.is_valid is False

    def test_missing_required_method_is_reported(self) -> None:
        expected = ModelExpectedStructure(
            class_name="NodeThingCompute", required_methods=("run",)
        )
        result = _validate(_WELL_FORMED_SOURCE, expected)
        assert result.structure_issues == (
            "class 'NodeThingCompute' missing required method 'run'",
        )
        assert result.is_valid is False

    def test_no_expected_structure_skips_structure_checks(self) -> None:
        result = _validate(_WELL_FORMED_SOURCE)
        assert result.structure_issues == ()


@pytest.mark.unit
class TestOverallValidity:
    def test_well_formed_artifact_is_valid(self) -> None:
        result = _validate(_WELL_FORMED_SOURCE)
        assert result.is_valid is True

    def test_stub_makes_artifact_invalid(self) -> None:
        result = _validate(_STUB_PASS_SOURCE)
        assert result.is_valid is False

    def test_valid_requires_both_no_stubs_and_no_structure_issues(self) -> None:
        expected = ModelExpectedStructure(class_name="NodeThingCompute")
        # Well-formed body + matching class name => valid.
        assert _validate(_WELL_FORMED_SOURCE, expected).is_valid is True


@pytest.mark.unit
class TestDeterminismAndPurity:
    def test_validation_is_deterministic(self) -> None:
        first = _validate(_STUB_TODO_COMMENT_SOURCE)
        second = _validate(_STUB_TODO_COMMENT_SOURCE)
        assert first == second

    def test_pure_function_matches_handler(self) -> None:
        via_fn = validate_generated_code(_WELL_FORMED_SOURCE)
        via_handler = _validate(_WELL_FORMED_SOURCE)
        assert via_fn == via_handler


@pytest.mark.unit
class TestContractStateCoverage:
    """Cover the contract-declared output states and published topic."""

    def test_contract_declares_all_outputs(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert set(contract["outputs"]) == {
            "parses",
            "syntax_error",
            "stub_methods",
            "structure_issues",
            "is_valid",
        }

    def test_contract_declares_bus_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        terminal = "onex.evt.omnimarket.generated-code-validation-completed.v1"
        assert contract["terminal_event"] == terminal
        assert terminal in contract["event_bus"]["publish_topics"]
