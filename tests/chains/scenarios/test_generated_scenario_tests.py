# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate: every node that declares chain_scenarios has a current generated test and valid receipts.

The generator writes ``<node>/tests/test_chain_scenarios.py`` from the node's contract.
To regenerate after an intended change run this file with ``CHAIN_GEN_WRITE=1``, then run
each node's generated test file with ``CHAIN_GEN_WRITE=1`` to keep the receipts, and
commit the diff. The core chain scenario validator judges the contracts and receipts.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validation.validator_chain_scenario import ValidatorChainScenario

from omnimarket.nodes.node_event_chain_generator_compute.handlers.handler_scenario_test_generator import (
    HandlerScenarioTestGenerator,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_scenario_test_generation import (
    ModelScenarioTestGenerationRequest,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_GENERATED = "test_chain_scenarios.py"


def _enrolled() -> list[Path]:
    return sorted(
        path
        for path in _NODES.glob("*/contract.yaml")
        if "chain_scenarios:" in path.read_text(encoding="utf-8")
    )


def _document(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def _generate(path: Path, document: dict[str, Any] | None = None) -> Any:
    return HandlerScenarioTestGenerator().handle(
        ModelScenarioTestGenerationRequest(
            node_name=path.parent.name,
            contract_document=document if document is not None else _document(path),
        )
    )


def test_the_three_proof_nodes_are_enrolled() -> None:
    assert {p.parent.name for p in _enrolled()} >= {
        "node_work_ledger_emit_effect",
        "node_golden_chain_sweep",
        "node_projection_work_ledger",
    }


@pytest.mark.parametrize("contract", _enrolled(), ids=lambda p: p.parent.name)
def test_the_generated_test_is_current(contract: Path) -> None:
    result = _generate(contract)
    assert result.status == "ok", result.issues
    target = contract.parent / "tests" / _GENERATED
    if os.environ.get("CHAIN_GEN_WRITE") == "1":
        target.parent.mkdir(exist_ok=True)
        (target.parent / "__init__.py").touch()
        target.write_text(result.test_source, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == result.test_source


@pytest.mark.parametrize("contract", _enrolled(), ids=lambda p: p.parent.name)
def test_the_core_validator_accepts_the_node_with_its_kept_receipts(
    contract: Path,
) -> None:
    result = ValidatorChainScenario().validate(contract)
    errors = [i for i in result.issues if i.severity == "error"]
    assert errors == [], [i.message for i in errors]


# --- failing controls: a scenario that cannot fail yields no test -----------------------


def _emit() -> tuple[Path, dict[str, Any]]:
    path = _NODES / "node_work_ledger_emit_effect" / "contract.yaml"
    return path, copy.deepcopy(_document(path))


def test_a_scenario_whose_cases_declare_no_break_is_refused() -> None:
    path, document = _emit()
    for case in document["chain_scenarios"]["cases"]:
        case["breaks"] = []
    result = _generate(path, document)
    assert result.status == "refused"
    assert result.test_source == ""
    assert any("case_without_named_breaks" in issue for issue in result.issues)


def test_a_scenario_that_takes_its_expectation_from_recorded_output_is_refused() -> (
    None
):
    path, document = _emit()
    document["chain_scenarios"]["cases"][0]["oracle"]["basis"] = "RECORDED_FROM_OUTPUT"
    result = _generate(path, document)
    assert result.status == "refused"
    assert any("expectation_not_independent" in issue for issue in result.issues)


def test_a_golden_case_missing_for_the_success_terminal_is_refused() -> None:
    path, document = _emit()
    document["chain_scenarios"]["cases"] = [
        c for c in document["chain_scenarios"]["cases"] if c["kind"] != "GOLDEN"
    ]
    result = _generate(path, document)
    assert result.status == "refused"
    assert any("success_terminal_without_golden_case" in i for i in result.issues)


def test_an_error_case_without_a_ledger_row_does_not_parse() -> None:
    path, document = _emit()
    del document["chain_scenarios"]["cases"][1]["ledger_failure_row"]
    result = _generate(path, document)
    assert result.status == "refused"
    assert any("scenario_malformed" in issue for issue in result.issues)


def test_the_projection_writers_error_half_is_a_refusal_that_says_why() -> None:
    document = _document(_NODES / "node_projection_work_ledger" / "contract.yaml")
    refusals = document["chain_scenarios"]["refusals"]
    assert [(r["applies_to"], r["kind"]) for r in refusals] == [
        ("ERROR", "FAILURE_NOT_TYPED")
    ]
    assert len(refusals[0]["reason"]) >= 40
