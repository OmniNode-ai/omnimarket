# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20675: contract -> bus -> compute -> typed result, plus the refusal chain.

Golden chain: typed requests go through the registered contract on the in-memory event bus
(RuntimeLocal) and the typed results come back with the closer's decisions. Error chain: a
request that names a kind without its inputs, or with inputs the kind does not take, is
refused before the handler runs, and a request the handler refuses ends the bus run FAILED
with no result.
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

import omnimarket.nodes.node_dod_closeout_decision_compute as node_package
from omnimarket.nodes.node_dod_closeout_decision_compute.models.model_dod_closeout_decision import (
    ModelDodCloseoutDecisionRequest,
    ModelDodCloseoutDecisionResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.dod-closeout-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.dod-closeout-decision-completed.v1"
FIXTURE = json.loads(
    (ROOT / "tests/fixtures/dod_closeout_decision_parity.json").read_text()
)


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


async def _run_over_the_bus(
    payload: dict[str, Any], tmp_path: Path
) -> tuple[EnumWorkflowResult, Any]:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    return await runtime.run_async(), runtime.handler_result


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeDodCloseoutDecisionCompute, handler_type)
    for side, model in (
        ("input_model", ModelDodCloseoutDecisionRequest),
        ("output_model", ModelDodCloseoutDecisionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_dod_closeout_decision_compute"].load() is node_package


def test_every_request_kind_is_declared_in_the_contract_input() -> None:
    declared = set(_contract()["inputs"]["kind"]["enum"])
    modelled = {
        k.value
        for k in __import__(
            "omnimarket.nodes.node_dod_closeout_decision_compute.models.model_dod_closeout_decision",
            fromlist=["EnumDodCloseoutDecisionKind"],
        ).EnumDodCloseoutDecisionKind
    }
    assert declared == modelled


@pytest.mark.asyncio
async def test_golden_chain_decides_a_ticket_over_the_bus(tmp_path: Path) -> None:
    n = next(
        i
        for i, e in enumerate(FIXTURE["expected"]["decide_ticket"])
        if e["ok"]["decision"] == "done"
    )
    payload = {"kind": "decide_ticket", **FIXTURE["cases"]["decide_ticket"][n]}
    outcome, result = await _run_over_the_bus(payload, tmp_path)
    assert outcome is EnumWorkflowResult.COMPLETED
    assert isinstance(result, ModelDodCloseoutDecisionResult)
    round_trip = ModelDodCloseoutDecisionResult.model_validate_json(
        result.model_dump_json()
    )
    assert round_trip == result
    assert result.status == "decided"
    assert result.decision is not None
    assert result.decision.decision == "done"
    assert result.decision.unmet == ()


@pytest.mark.asyncio
async def test_golden_chain_refuses_a_symbol_grep_binding_over_the_bus(
    tmp_path: Path,
) -> None:
    n = next(
        i
        for i, e in enumerate(FIXTURE["expected"]["refusal_of"])
        if e["ok"]["refusal"] == "symbol-grep-only"
    )
    payload = {"kind": "refusal_of", **FIXTURE["cases"]["refusal_of"][n]}
    outcome, result = await _run_over_the_bus(payload, tmp_path)
    assert outcome is EnumWorkflowResult.COMPLETED
    assert isinstance(result, ModelDodCloseoutDecisionResult)
    assert result.binding_refusal is not None
    assert result.binding_refusal.refusal == "symbol-grep-only"


@pytest.mark.asyncio
async def test_golden_chain_decides_a_chunk_over_the_bus(tmp_path: Path) -> None:
    payload = {"kind": "decide_chunk", **FIXTURE["cases"]["decide_chunk"][0]}
    outcome, result = await _run_over_the_bus(payload, tmp_path)
    assert outcome is EnumWorkflowResult.COMPLETED
    assert isinstance(result, ModelDodCloseoutDecisionResult)
    assert result.decisions is not None
    assert [d["decision"] for d in result.decisions] == ["done"]
    assert result.flips is not None
    assert [f["id"] for f in result.flips] == ["OMN-1"]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "decide_ticket"}, "decide_ticket requires ticket"),
        ({"kind": "refusal_of", "check": {}, "ticket": {}}, "does not take ticket"),
        ({"kind": "parse_args"}, "parse_args requires args"),
        ({"kind": "chunk_list", "items": ["a"], "size": 0}, "greater than or equal"),
        ({"kind": "chunk_list", "items": ["a"]}, "chunk_list requires size"),
        ({"kind": "merged_since", "date": "10/03/2026"}, "pattern"),
        (
            {"kind": "select_candidates", "candidates": [], "max_candidates": -1},
            "greater than or equal",
        ),
        ({"kind": "read_dod_verify", "text_state": "x"}, "requires receipt_json"),
        ({"kind": "no_such_decision"}, "kind"),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelDodCloseoutDecisionRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_does_not_complete(tmp_path: Path) -> None:
    outcome, result = await _run_over_the_bus({"kind": "decide_ticket"}, tmp_path)
    assert outcome is EnumWorkflowResult.FAILED
    assert result is None


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_when_the_handler_refuses(
    tmp_path: Path,
) -> None:
    payload = {"kind": "parse_args", "args": {"date": "10/03/2026"}}
    outcome, result = await _run_over_the_bus(payload, tmp_path)
    assert outcome is EnumWorkflowResult.FAILED
    assert result is None
