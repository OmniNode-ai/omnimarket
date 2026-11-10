# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger reducer node, contract to bus to result (OMN-22915).

Golden chain: a shadow-judge request, a guard-replay request and a rebuild request each go through the
registered contract on the in-memory bus and return their typed result. Error chain: a request that names
no sub-request, or the wrong one, is refused before the handler runs, and a rebuild whose feed cannot be one
table fails the run with no result. The handlers' own cases are in tests/unit/nodes.
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

import omnimarket.nodes.node_work_ledger_reducer_compute as node_package
from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_reducer import (
    ModelWorkLedgerReducerRequest,
    ModelWorkLedgerReducerResult,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.work-ledger-reducer-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.work-ledger-reducer-completed.v1"
LEDGER = next(iter(GOVERNED_LEDGER_NAMES))
ACCEPT = {"verdict": "accept"}
HOLD = "2026-09-28T10:10:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved"
HOLD_REUSED_ID = "2026-09-28T10:30:00Z | HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-x | until=2026-09-28T12:00:00Z | again"
CLAIM = "2026-09-28T09:30:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work"


def _event(row: str, **fields: Any) -> dict[str, Any]:
    return {
        "ledger_id": "rolling-work-ledger",
        "row_id": work_ledger_row_id(row),
        "raw_row": row,
        **fields,
    }


async def _run(
    tmp_path: Path, payload: dict[str, Any], name: str
) -> tuple[RuntimeLocal, Any]:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / f"state-{name}",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    return runtime, await runtime.run_async()


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeWorkLedgerReducerCompute, handler_type)
    for side, model in (
        ("input_model", ModelWorkLedgerReducerRequest),
        ("output_model", ModelWorkLedgerReducerResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_work_ledger_reducer_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_shadow_judge_names_the_difference_over_the_bus(
    tmp_path: Path,
) -> None:
    runtime, outcome = await _run(
        tmp_path,
        {
            "operation": "shadow_judge",
            "shadow": {
                "row_ref": "req-1",
                "row": HOLD_REUSED_ID,
                "existing": HOLD + "\n",
                "ledger_name": LEDGER,
                "live": ACCEPT,
            },
        },
        "shadow",
    )
    assert outcome is not EnumWorkflowResult.FAILED
    result = runtime.handler_result
    assert isinstance(result, ModelWorkLedgerReducerResult)
    assert result.shadow is not None
    assert result.shadow.agrees is False
    assert result.shadow.shadow.rule is not None
    assert result.shadow.shadow.rule.startswith("OMN-19256 row grammar")
    assert (
        result.shadow.difference
        == f"live accepted; shadow refused under {result.shadow.shadow.rule}"
    )
    assert (
        ModelWorkLedgerReducerResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_golden_chain_guard_replay_reports_a_changed_verdict_over_the_bus(
    tmp_path: Path,
) -> None:
    runtime, _ = await _run(
        tmp_path,
        {
            "operation": "guard_replay",
            "replay": {
                "ledger_name": LEDGER,
                "entries": [
                    {"row_ref": "a", "row": HOLD, "recorded": ACCEPT},
                    {"row_ref": "b", "row": HOLD_REUSED_ID, "recorded": ACCEPT},
                ],
            },
        },
        "replay",
    )
    result = runtime.handler_result
    assert isinstance(result, ModelWorkLedgerReducerResult)
    assert result.replay is not None
    assert result.replay.judged == 2
    assert [d.row_ref for d in result.replay.differences] == ["b"]
    assert result.replay.equivalent is False


@pytest.mark.asyncio
async def test_golden_chain_rebuild_over_the_bus(tmp_path: Path) -> None:
    runtime, _ = await _run(
        tmp_path,
        {
            "operation": "rebuild",
            "rebuild": {
                "events": [
                    _event(CLAIM, source="bootstrap", legacy_ordinal=1),
                    _event(HOLD, source="live", ledger_seq=1),
                ]
            },
        },
        "rebuild",
    )
    result = runtime.handler_result
    assert isinstance(result, ModelWorkLedgerReducerResult)
    assert result.rebuild is not None
    assert [r.ledger_seq for r in result.rebuild.rebuilt.rows] == [1, None]
    assert {e.entity_key for e in result.rebuild.rebuilt.state} == {
        "claim:alpha",
        "hold:2026-09-28T10:10:00Z-beta",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"operation": "other"},
        {"operation": "shadow_judge"},
        {
            "operation": "rebuild",
            "rebuild": {"events": []},
            "replay": {"ledger_name": LEDGER, "entries": []},
        },
        {
            "operation": "guard_replay",
            "replay": {"ledger_name": LEDGER, "entries": []},
            "surprise": 1,
        },
    ],
)
def test_error_chain_refuses_before_the_handler_runs(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelWorkLedgerReducerRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_error_chain_a_rebuild_that_cannot_be_one_table_fails_with_no_result(
    tmp_path: Path,
) -> None:
    bad = _event(HOLD, source="live", ledger_seq=1)
    bad["row_id"] = "0" * 64
    runtime, outcome = await _run(
        tmp_path, {"operation": "rebuild", "rebuild": {"events": [bad]}}, "bad-rebuild"
    )
    assert outcome is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None


@pytest.mark.asyncio
async def test_error_chain_a_malformed_request_fails_with_no_result(
    tmp_path: Path,
) -> None:
    runtime, outcome = await _run(tmp_path, {"operation": "shadow_judge"}, "malformed")
    assert outcome is EnumWorkflowResult.FAILED
    assert runtime.handler_result is None
