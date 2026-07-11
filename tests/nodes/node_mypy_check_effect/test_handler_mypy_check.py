# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol tests for node_mypy_check_effect — real mypy over a code artifact.

The EFFECT genuinely shells out to ``mypy`` (a repo dev tool), so the tests
exercise real behavior: a type-clean artifact yields ``success=True`` with zero
errors, a type-erroring artifact yields ``success=False`` with at least one
``error`` diagnostic, the ``path`` branch checks an on-disk file, and the
request model enforces exactly-one-of ``source_text``/``path``. The clean-case
test also asserts ``mypy_available is True`` so a missing checker fails the
suite rather than passing vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_mypy_check_effect.handlers.handler_mypy_check import (
    HandlerMypyCheck,
    check_artifact,
)
from omnimarket.nodes.node_mypy_check_effect.models.model_mypy_check_request import (
    ModelMypyCheckRequest,
)
from omnimarket.nodes.node_mypy_check_effect.models.model_mypy_check_result import (
    ModelMypyCheckResult,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_mypy_check_effect"
    / "contract.yaml"
)

_CLEAN_SOURCE = """\
def add(first: int, second: int) -> int:
    return first + second
"""

_TYPE_ERROR_SOURCE = """\
def add(first: int, second: int) -> str:
    return first + second
"""


def _check(source: str) -> ModelMypyCheckResult:
    return HandlerMypyCheck().handle(ModelMypyCheckRequest(source_text=source))


@pytest.mark.unit
class TestMypyCheck:
    def test_clean_artifact_has_zero_errors(self) -> None:
        result = _check(_CLEAN_SOURCE)
        assert result.mypy_available is True
        assert result.error_count == 0
        assert result.success is True

    def test_type_erroring_artifact_is_flagged(self) -> None:
        result = _check(_TYPE_ERROR_SOURCE)
        assert result.mypy_available is True
        assert result.error_count >= 1
        assert result.success is False
        assert any(diag.severity == "error" for diag in result.diagnostics)

    def test_diagnostic_fields_are_typed(self) -> None:
        result = _check(_TYPE_ERROR_SOURCE)
        error = next(diag for diag in result.diagnostics if diag.severity == "error")
        assert error.line >= 1
        assert error.column is None or error.column >= 0
        assert error.message


@pytest.mark.unit
class TestPathBranch:
    def test_path_target_is_checked(self, tmp_path: Path) -> None:
        artifact = tmp_path / "artifact.py"
        artifact.write_text(_TYPE_ERROR_SOURCE)
        result = check_artifact(ModelMypyCheckRequest(path=str(artifact)))
        assert result.mypy_available is True
        assert result.error_count >= 1
        assert result.success is False


@pytest.mark.unit
class TestRequestValidation:
    def test_neither_target_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ModelMypyCheckRequest()

    def test_both_targets_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            ModelMypyCheckRequest(source_text="x = 1\n", path="/tmp/x.py")

    def test_empty_source_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            ModelMypyCheckRequest(source_text="   \n")


@pytest.mark.unit
class TestContractStateCoverage:
    """Cover the contract-declared output states and published topic."""

    def test_contract_declares_all_outputs(self) -> None:
        import yaml

        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        assert set(contract["outputs"]) == {
            "success",
            "error_count",
            "diagnostics",
            "mypy_available",
        }

    def test_contract_declares_bus_topics(self) -> None:
        import yaml

        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        terminal = "onex.evt.omnimarket.mypy-check-completed.v1"
        assert contract["terminal_event"] == terminal
        assert terminal in contract["event_bus"]["publish_topics"]
