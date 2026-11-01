# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger row grammar node: parity with the grammar it was ported from, contract to bus to result.

``tests/fixtures/ledger_row_grammar_parity.json`` holds synthetic rows beside the answers the
original grammar gave for them (row refusals, the first refusal of a payload, the first refusal of a
payload against written ledger text), and the grammar's own JSON. The node's answers must equal them.
Golden chain: a request through the registered contract on the in-memory bus. Error chain: a malformed
request is refused with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_ledger_row_grammar_compute as node_package
from omnimarket.nodes.node_ledger_row_grammar_compute.handlers.handler_ledger_row_grammar import (
    HandlerLedgerRowGrammar,
)
from omnimarket.nodes.node_ledger_row_grammar_compute.models.model_ledger_row_grammar import (
    ModelLedgerRowGrammarRequest,
    ModelLedgerRowGrammarResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.ledger-row-grammar-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.ledger-row-grammar-completed.v1"
PARITY = json.loads(
    (ROOT / "tests/fixtures/ledger_row_grammar_parity.json").read_text(encoding="utf-8")
)
HANDLER = HandlerLedgerRowGrammar()


def ask(**fields: Any) -> ModelLedgerRowGrammarResult:
    return HANDLER.handle(ModelLedgerRowGrammarRequest.model_validate(fields))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeLedgerRowGrammarCompute, handler_type)
    for side, model in (
        ("input_model", ModelLedgerRowGrammarRequest),
        ("output_model", ModelLedgerRowGrammarResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_ledger_row_grammar_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["row_cases"], ids=lambda c: c["row"][:60])
def test_row_refusals_match_the_original_grammar(case: dict[str, Any]) -> None:
    result = ask(operation="row_refusals", line=case["row"])
    assert result.refusals == case["refusals"]
    assert result.admitted is (not case["refusals"])
    assert result.grammar_version == PARITY["grammar_version"]


@pytest.mark.parametrize(
    "case", PARITY["payload_cases"], ids=lambda c: c["payload"][:60]
)
def test_payload_refusals_match_the_original_grammar(case: dict[str, Any]) -> None:
    result = ask(
        operation="payload_refusal",
        payload=case["payload"],
        ledger_name=case["ledger_name"],
    )
    assert result.refusal == case["refusal"]
    assert result.admitted is (case["refusal"] is None)


@pytest.mark.parametrize("case", PARITY["state_cases"], ids=lambda c: c["payload"][:60])
def test_state_refusals_match_the_original_grammar(case: dict[str, Any]) -> None:
    result = ask(
        operation="state_refusal",
        payload=case["payload"],
        existing=case["existing"],
        ledger_name=case["ledger_name"],
    )
    assert result.refusal == case["refusal"]
    assert result.admitted is (case["refusal"] is None)


def test_the_parity_fixture_exercises_both_outcomes_of_every_operation() -> None:
    assert any(c["refusals"] for c in PARITY["row_cases"])
    assert any(not c["refusals"] for c in PARITY["row_cases"])
    assert any(c["refusal"] for c in PARITY["payload_cases"])
    assert any(not c["refusal"] for c in PARITY["payload_cases"])
    assert any(c["refusal"] for c in PARITY["state_cases"])
    assert any(not c["refusal"] for c in PARITY["state_cases"])


def test_the_grammar_json_equals_the_original() -> None:
    result = ask(operation="grammar")
    assert result.grammar_json == PARITY["grammar_json"]
    assert json.loads(result.grammar_json or "")["version"] == result.grammar_version


def test_an_ungoverned_ledger_name_is_not_judged() -> None:
    result = ask(
        operation="payload_refusal",
        payload="2026-10-15T06:00:00Z | WIBBLE | lane=lane-a | nonsense",
        ledger_name="scratch.md",
    )
    assert result.admitted is True
    assert result.refusal is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "other"},
        {"operation": "row_refusals", "line": 5},
        {"operation": "grammar", "surprise": 1},
    ],
)
def test_error_chain_refuses_before_the_handler_runs(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelLedgerRowGrammarRequest.model_validate(payload)


async def _run(tmp_path: Path, payload: dict[str, Any], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_the_original(tmp_path: Path) -> None:
    case = next(c for c in PARITY["row_cases"] if c["refusals"])
    runtime = await _run(
        tmp_path, {"operation": "row_refusals", "line": case["row"]}, "row"
    )
    result = runtime.handler_result
    assert isinstance(result, ModelLedgerRowGrammarResult)
    assert result.refusals == case["refusals"]
    assert result.admitted is False
    assert (
        ModelLedgerRowGrammarResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {}, "empty")
    assert runtime.handler_result is None
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=tmp_path / "bad.json",
        state_root=tmp_path / "state-bad",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    (tmp_path / "bad.json").write_text(
        json.dumps({"operation": "row_refusals", "line": 5})
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
