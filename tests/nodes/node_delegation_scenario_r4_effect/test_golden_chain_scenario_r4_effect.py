# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R4 (OMN-23173): one run writes one STATUS row and emits one result on the bus.

The golden chains run the scenario against loopback stub servers called by the real delegation effect, and against the
scripted in-memory transport. The error chains are named injected breaks: each makes exactly one thing wrong and shows
the scenario red for that reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.delegation_readiness_probe import (
    LoopbackStubServers,
    ProtocolLegTransport,
    ScriptedLegTransport,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    JournalScenarioEventSink,
    render_systemd,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_module,
)
from omnimarket.nodes.node_delegation_scenario_r4_effect.handlers.handler_scenario_r4 import (
    CONTRACT_PATH,
    NODE_DIR,
    HandlerScenarioR4,
    ModelScenarioR4RunCommand,
    load_runtime,
    load_schedule,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 13, 6, 30, tzinfo=UTC)
RUNTIME = load_runtime()
INPUTS = NODE_DIR / RUNTIME.inputs_relpath
ABSENT_LEG = "slow_then_absent_then_healthy"


class Rows:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def __call__(self, row: str) -> None:
        self.rows.append(row)


class Breakable(ScriptedLegTransport):
    """The scripted transport with one named wrong behaviour of the product."""

    def __init__(self, brk: str | None = None) -> None:
        super().__init__()
        self.brk = brk

    def effect(
        self,
    ) -> Callable[[ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult]:
        inner = super().effect()
        scripts = self._scripts

        def call(
            request: ModelLlmDelegationCallRequest,
        ) -> ModelLlmDelegationCallResult:
            result = inner(request)
            rung = scripts[request.endpoint_ref]
            if self.brk == "killed_is_unknown" and rung.behaviour == "killed":
                return result.model_copy(
                    update={"failure_class": EnumDelegationFailureClass.UNKNOWN}
                )
            if self.brk == "healthy_rung_down" and rung.behaviour == "answer":
                return result.model_copy(
                    update={
                        "success": False,
                        "content": "",
                        "failure_class": EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                        "http_status": 503,
                    }
                )
            if self.brk == "wrong_bytes" and rung.behaviour == "answer":
                return result.model_copy(
                    update={
                        "content": "### ANSWER\n"
                        + "A different but long enough reply about something else entirely. "
                        * 6
                    }
                )
            return result

        return call


def build(
    tmp_path: Path,
    transport: Callable[[], ProtocolLegTransport],
    *,
    inputs_path: Path = INPUTS,
    appender: Callable[[str], None] | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR4, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    handler = HandlerScenarioR4(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=appender or rows,
        sink=events,
        terminal_topic=TERMINAL,
        transport_factory=transport,
        trigger=trigger,
        clock=lambda: NOW,
        inputs_path=inputs_path,
    )
    return handler, rows, events


def failed_assertions(assertions: tuple[str, ...]) -> list[str]:
    return [item for item in assertions if item.startswith("-")]


def test_golden_chain_inmemory_every_assertion_held(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(tmp_path, ScriptedLegTransport)

    result = handler.handle(ModelScenarioR4RunCommand())

    assert failed_assertions(result.assertions) == []
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert "+stopped_then_healthy_typed" in result.assertions
    assert f"+{ABSENT_LEG}_walked_the_reachable_rungs" in result.assertions
    assert f"+{ABSENT_LEG}_absent_rung_not_attempted" in result.assertions
    assert "+every_down_reason_in_closed_list" in result.assertions
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R4"
    assert parsed.trigger is EnumScenarioTrigger.SCHEDULE
    assert parsed.slot_start == datetime(
        2026, 11, 9, 12, 0, tzinfo=UTC
    )  # the six-hourly slot of 13:06
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R4")
    ]


def test_golden_chain_real_loopback_stubs_classify_stopped_killed_and_slow_rungs(
    tmp_path: Path,
) -> None:
    """The real backend: production's delegation effect calls real stub servers, so the classes are production's own."""
    handler, rows, events = build(tmp_path, LoopbackStubServers)

    result = handler.run()

    assert failed_assertions(result.assertions) == [], result.detail
    for leg in (
        "stopped_then_healthy",
        "killed_then_healthy",
        "slow_then_healthy",
        ABSENT_LEG,
    ):
        for check in (
            "completed",
            "walked_the_reachable_rungs",
            "typed",
            "healthy_accepted",
            "inside_ceiling",
            "answer_bytes",
        ):
            assert f"+{leg}_{check}" in result.assertions
    assert "+every_down_returned_to_caller" in result.assertions
    assert "+every_down_savings_zero" in result.assertions
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_golden_chain_manual_trigger_is_recorded_as_manual(tmp_path: Path) -> None:
    handler, rows, _ = build(
        tmp_path, ScriptedLegTransport, trigger=EnumScenarioTrigger.MANUAL
    )
    handler.run()
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.trigger is EnumScenarioTrigger.MANUAL


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR4(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=Rows(),
        sink=JournalScenarioEventSink(journal),
        terminal_topic=TERMINAL,
        transport_factory=ScriptedLegTransport,
        clock=lambda: NOW,
    )
    handler.run()
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["topic"] == TERMINAL


# -- error chains: one named injected break each -------------------------------------------------------------------


def test_error_chain_a_killed_rung_typed_unknown_turns_the_killed_leg_red(
    tmp_path: Path,
) -> None:
    handler, rows, _ = build(tmp_path, lambda: Breakable("killed_is_unknown"))
    result = handler.run()
    assert "-killed_then_healthy_typed" in result.assertions
    assert "+stopped_then_healthy_typed" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "killed_then_healthy_typed" in result.detail
    assert len(rows.rows) == 1


def test_error_chain_a_healthy_rung_that_does_not_answer_turns_every_recover_leg_red(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("healthy_rung_down"))
    result = handler.run()
    for leg in ("stopped_then_healthy", "killed_then_healthy", "slow_then_healthy"):
        assert f"-{leg}_completed" in result.assertions
        assert f"-{leg}_healthy_accepted" in result.assertions


def test_error_chain_a_wrong_answer_is_caught_by_the_byte_comparison_not_the_gate(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("wrong_bytes"))
    result = handler.run()
    assert "-slow_then_healthy_answer_bytes" in result.assertions
    assert "-stopped_then_healthy_answer_bytes" in result.assertions


def test_error_chain_a_ceiling_the_run_cannot_meet_turns_the_ceiling_assertion_red(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["class_ceiling_ms"] = 1
    low = tmp_path / "low.yaml"
    low.write_text(yaml.safe_dump(raw), encoding="utf-8")
    handler, _, _ = build(tmp_path, ScriptedLegTransport, inputs_path=low)
    result = handler.run()
    assert "-stopped_then_healthy_inside_ceiling" in result.assertions
    assert "+stopped_then_healthy_completed" in result.assertions


def test_error_chain_a_ladder_where_a_rung_is_wrong_for_the_leg_turns_walked_the_reachable_rungs_red(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["recover_legs"][0]["rungs"][0]["behaviour"] = "answer"
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    handler, _, _ = build(tmp_path, ScriptedLegTransport, inputs_path=broken)
    result = handler.run()
    assert "-stopped_then_healthy_walked_the_reachable_rungs" in result.assertions
    assert "-stopped_then_healthy_typed" in result.assertions


def test_error_chain_unreadable_inputs_is_a_failure_and_still_writes_one_row(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(
        tmp_path, ScriptedLegTransport, inputs_path=tmp_path / "missing.yaml"
    )
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_error_chain_a_walk_that_ignores_the_declaration_attempts_the_absent_rung(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break: the walk hands out every rung, declared credential or not; the absent rung would answer, so the leg is red."""
    real_walk = port_module._ProbeLadderWalk

    class IgnoresDeclaration(real_walk):  # type: ignore[misc, valid-type]
        def __init__(self, ladder: object) -> None:
            plain = ModelProbeLadder(
                rungs=tuple(
                    ModelProbeRung.model_validate(
                        rung.model_dump(exclude={"credential_resolved"})
                    )
                    for rung in ladder.rungs  # type: ignore[attr-defined]
                )
            )
            super().__init__(plain)

    monkeypatch.setattr(port_module, "_ProbeLadderWalk", IgnoresDeclaration)
    handler, _, _ = build(tmp_path, ScriptedLegTransport)
    result = handler.run()
    assert f"-{ABSENT_LEG}_healthy_accepted" in result.assertions
    assert f"-{ABSENT_LEG}_walked_the_reachable_rungs" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE


def test_error_chain_an_absent_rung_that_the_walk_attempts_names_itself(
    tmp_path: Path,
) -> None:
    """A leg whose declared absent rung is flagged resolved is walked in full: no absent-rung assertion is made at all."""
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    for rung in raw["credential_absent_leg"]["rungs"]:
        rung.pop("credential_resolved", None)
    broken = tmp_path / "all_resolved.yaml"
    broken.write_text(yaml.safe_dump(raw), encoding="utf-8")
    handler, _, _ = build(tmp_path, ScriptedLegTransport, inputs_path=broken)
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)


def test_error_chain_a_ledger_append_that_fails_raises_and_emits_nothing(
    tmp_path: Path,
) -> None:
    def refuse(row: str) -> None:
        raise RuntimeError("the ledger append was rejected")

    handler, _, events = build(tmp_path, ScriptedLegTransport, appender=refuse)
    with pytest.raises(RuntimeError, match="ledger append"):
        handler.run()
    assert events.events == []


def test_error_chain_a_bus_emit_that_fails_raises_after_the_row_is_written(
    tmp_path: Path,
) -> None:
    class Down:
        def emit(self, topic: str, payload: Mapping[str, object]) -> None:
            raise RuntimeError("the bus emit failed")

    rows = Rows()
    handler = HandlerScenarioR4(
        RUNTIME,
        home=tmp_path,
        environment={},
        appender=rows,
        sink=Down(),
        terminal_topic=TERMINAL,
        transport_factory=ScriptedLegTransport,
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="bus emit"):
        handler.run()
    assert len(rows.rows) == 1


def test_the_schedule_block_renders_a_six_hourly_user_timer() -> None:
    schedule = load_schedule(CONTRACT_PATH)
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r4_effect",
        omni_root=Path("/opt/omni"),
        home=Path("/home/lab"),
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={"ONEX_HOST_NAME": "lab-a"},
    )
    assert "OnCalendar=*-*-* 00,06,12,18:20:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r4_effect run" in service
    assert 'Environment="ONEX_HOST_NAME=lab-a"' in service
