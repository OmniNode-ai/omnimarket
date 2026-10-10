# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20680: the hourly tick decisions, contract to bus to typed result, parity and refusals.

Parity: tests/fixtures/hourly_tick_parity.json holds 60 cases captured from the
pre-conversion tick (source commit recorded in the file). Golden chain: probe then record
through the registered contract on the in-memory bus. Error chain: every refusal the old
tick made before any write is a refusal here.
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

import omnimarket.nodes.node_hourly_tick_decision_compute as node_package
from omnimarket.nodes.node_hourly_tick_decision_compute.handlers.handler_hourly_tick_decision import (
    HandlerHourlyTickDecision,
    checkpoint_line,
    completion_line,
    facts_from_tail,
    result_payload,
    tick_outcome,
)
from omnimarket.nodes.node_hourly_tick_decision_compute.models.model_hourly_tick_decision import (
    ModelHourlyTickDecisionRequest,
    ModelHourlyTickDecisionResult,
    ModelSweepResult,
    ModelTickState,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.hourly-tick-decision-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.hourly-tick-decision-completed.v1"
PARITY = json.loads((ROOT / "tests/fixtures/hourly_tick_parity.json").read_text())
HANDLER = HandlerHourlyTickDecision()

CHECKPOINTS = (
    "- 2026-09-16T21:27:48Z 60m checkpoint: earlier\n"
    "- 2026-09-16T21:30:00Z operator mentioned 60m checkpoint later\n"
)
POINTER = "2026-09-16T21:20:00Z\n"
LEDGER = (
    "2026-09-16T21:27:48Z | CLAIM | lane=excluded | before\n"
    "2026-09-16T21:30:00Z | TERMINAL | lane=codex:whole ticket=x | prose lane=wrong\n"
    "2026-09-16T21:31:00Z | CLAIM | lane=other | body\n"
    "2026-09-16T21:40:57Z | TERMINAL | lane=last | body\n"
    "2026-09-16T21:40:58Z | CLAIM | lane=excluded | after\n"
)


def probe(**overrides: Any) -> dict[str, Any]:
    return {
        "kind": "probe",
        "now": "2026-09-16T21:40:57Z",
        "checkpoint_text": CHECKPOINTS,
        "sweep_pointer_text": POINTER,
        "ledger_text": LEDGER,
        "fireId": "fire-123",
        **overrides,
    }


def _contract() -> dict[str, Any]:
    return dict(yaml.safe_load((NODE_DIR / "contract.yaml").read_text()))


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract = _contract()
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [
        COMMAND_TOPIC,
        "onex.intent.platform.runtime-tick.v1",
    ]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeHourlyTickDecisionCompute, handler_type)
    for side, model in (
        ("input_model", ModelHourlyTickDecisionRequest),
        ("output_model", ModelHourlyTickDecisionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_hourly_tick_decision_compute"].load() is node_package


@pytest.mark.parametrize("case", PARITY["cases"])
def test_old_behavior_parity(case: dict[str, Any]) -> None:
    state = ModelTickState.model_validate(case["state"])
    sweep = (
        ModelSweepResult.model_validate(case["sweep"])
        if case["sweep"] is not None
        else None
    )
    assert checkpoint_line(state) == case["checkpoint"]
    assert completion_line(state, sweep, str(case["run_id"])) == case["completion"]
    assert tick_outcome(sweep) == case["outcome"]
    assert result_payload("fire-123", sweep) == case["result"]
    assert facts_from_tail(state.tail, 12) == case["facts"]


def test_parity_fixture_exercises_completed_and_every_degraded_class() -> None:
    seen = {(c["outcome"]["phase"], c["outcome"]["class"]) for c in PARITY["cases"]}
    assert ("completed", None) in seen
    assert {k for p, k in seen if p == "degraded"} == {
        "floor_refused",
        "truncated",
        "unsourced",
        "transport",
        "timeout",
        "unclassified",
    }


def test_probe_window_counts_lanes_and_sweep_request() -> None:
    result = HANDLER.handle(ModelHourlyTickDecisionRequest.model_validate(probe()))
    assert result.phase == "prepared"
    assert (result.state.rows, result.state.terminal) == (3, 2)
    assert result.state.lanes == "lane=codex:whole,lane=last"
    assert result.state.ledger_total == 5
    assert result.state.prev == "2026-09-16T21:27:48Z"
    assert result.sweep_request is not None
    assert result.sweep_request.since == "2026-09-16T21:20:00Z"
    assert "operator mentioned" in result.sweep_request.facts
    assert result.checkpoint_line == checkpoint_line(result.state)
    assert result.facts_source == "derived from the last 12 checkpoint lines"
    assert result.completion_line is None


def test_probe_caller_facts_take_precedence() -> None:
    result = HANDLER.handle(
        ModelHourlyTickDecisionRequest.model_validate(probe(facts="given"))
    )
    assert result.sweep_request is not None
    assert result.sweep_request.facts == "given"
    assert result.facts_source == "caller-supplied"


def test_skipped_sweep_completes_at_probe_without_a_sweep_request() -> None:
    result = HANDLER.handle(
        ModelHourlyTickDecisionRequest.model_validate(probe(skipSweep=True, runId="r1"))
    )
    assert result.sweep_request is None
    assert result.phase == "completed"
    assert result.completion_line is not None
    assert "hourly-tick run=r1" in result.completion_line
    assert result.result_payload is not None
    assert result.result_payload["fire_id"] == "fire-123"


def test_record_degrades_when_something_needed_a_reply_and_nothing_was_accepted() -> (
    None
):
    prepared = HANDLER.handle(ModelHourlyTickDecisionRequest.model_validate(probe()))
    done = HANDLER.handle(
        ModelHourlyTickDecisionRequest.model_validate(
            {
                "kind": "record",
                "state": prepared.state.model_dump(),
                "sweep": {
                    "needing": 3,
                    "accepted": 0,
                    "held": 3,
                    "held_by_class": {"transport": 3},
                },
                "fireId": "fire-123",
            }
        )
    )
    assert done.phase == "degraded"
    assert done.degraded is True
    assert done.held_class == "transport"
    assert done.completion_line is not None
    assert "held 3, phase=degraded class=transport" in done.completion_line
    assert done.result_payload is not None
    assert done.result_payload["class"] == "transport"


@pytest.mark.parametrize(
    ("file", "value"),
    [
        ("sweep_pointer_text", "bad"),
        ("ledger_text", "no timestamps"),
        ("checkpoint_text", "no checkpoint"),
    ],
)
def test_probe_refuses_invalid_or_missing_state(file: str, value: str) -> None:
    request = ModelHourlyTickDecisionRequest.model_validate(probe(**{file: value}))
    with pytest.raises(ValueError, match="hourly-tick"):
        HANDLER.handle(request)


@pytest.mark.parametrize("tail", [[], ["", " "]])
def test_empty_facts_refused(tail: list[str]) -> None:
    with pytest.raises(ValueError, match="no facts supplied"):
        facts_from_tail(tail, 12)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "probe"}, "probe requires"),
        (probe(now="bad"), "now"),
        (probe(fireId="../escape"), "fireId|fire_id"),
        (probe(tailLines=0), "greater"),
        (probe(state=None, sweep={"needing": 1}), "does not take state or sweep"),
        ({"kind": "record"}, "record requires the prepared state"),
        (
            {"kind": "record", "state": {"now": "x"}, "ledger_text": "z"},
            "now|prev",
        ),
    ],
)
def test_error_chain_refuses_before_the_handler_runs(
    payload: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelHourlyTickDecisionRequest.model_validate(payload)


def test_record_refuses_unexpected_probe_inputs() -> None:
    prepared = HANDLER.handle(ModelHourlyTickDecisionRequest.model_validate(probe()))
    with pytest.raises(ValidationError, match="record does not take ledger_text"):
        ModelHourlyTickDecisionRequest.model_validate(
            {
                "kind": "record",
                "state": prepared.state.model_dump(),
                "ledger_text": LEDGER,
            }
        )


async def _run(tmp_path: Path, payload: dict[str, Any]) -> RuntimeLocal:
    input_path = tmp_path / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_probe_then_record_over_the_bus(tmp_path: Path) -> None:
    (tmp_path / "probe").mkdir()
    probed = await _run(tmp_path / "probe", probe())
    prepared = probed.handler_result
    assert isinstance(prepared, ModelHourlyTickDecisionResult)
    assert prepared.phase == "prepared"
    assert prepared.state.rows == 3
    (tmp_path / "record").mkdir()
    recorded = await _run(
        tmp_path / "record",
        {
            "kind": "record",
            "state": json.loads(prepared.state.model_dump_json()),
            "sweep": {
                "needing": 2,
                "accepted": 0,
                "held": 2,
                "held_by_class": {"timeout": 2},
            },
            "fireId": "fire-123",
        },
    )
    done = recorded.handler_result
    assert isinstance(done, ModelHourlyTickDecisionResult)
    assert (done.phase, done.held_class) == ("degraded", "timeout")
    assert (
        ModelHourlyTickDecisionResult.model_validate_json(done.model_dump_json())
        == done
    )


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, {"kind": "probe"})
    assert runtime.handler_result is None
    (tmp_path / "state2").mkdir()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(probe(sweep_pointer_text="bad")))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "state2",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
