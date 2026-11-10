# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R6 (OMN-23174): one run writes one STATUS row and emits one result on the bus.

The golden chain runs the scenario on the scripted in-memory transport: the one rung is scripted to answer after 300 s and
the transport reports that time as the run's duration, so no test sleeps. The real loopback backend is the production
setting and is run on the lab; no test here runs it for 300 s. The error chains are named injected breaks: each makes
exactly one thing wrong and shows the scenario red for that reason, with the same input green without the break.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_readiness_probe import (
    ProtocolLegTransport,
    ScriptedLegTransport,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    JournalScenarioEventSink,
    render_systemd,
)
from omnimarket.handlers.node_overlay_reader import OVERLAY_ROOTS_ENV
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r6_effect.handlers.handler_scenario_r6 import (
    CONTRACT_PATH,
    NODE_DIR,
    HandlerScenarioR6,
    ModelScenarioR6RunCommand,
    load_runtime,
    load_schedule,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 3, 41, 30, tzinfo=UTC)
RUNTIME = load_runtime()
INPUTS = NODE_DIR / RUNTIME.inputs_relpath


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

        def call(
            request: ModelLlmDelegationCallRequest,
        ) -> ModelLlmDelegationCallResult:
            result = inner(request)
            if self.brk == "wrong_bytes":
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
    transport: Callable[[], ProtocolLegTransport] = ScriptedLegTransport,
    *,
    inputs_path: Path = INPUTS,
    appender: Callable[[str], None] | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR6, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    handler = HandlerScenarioR6(
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


def changed_inputs(tmp_path: Path, **changes: object) -> Path:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw.update(changes)
    path = tmp_path / "changed.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_golden_chain_every_assertion_held_and_one_row_and_one_event(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(tmp_path)

    result = handler.handle(ModelScenarioR6RunCommand())

    assert failed_assertions(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    for name in (
        "long_completed",
        "long_duration_over_contract_ceiling",
        "long_ceiling_from_overlay",
        "long_budget_source_overlay",
        "long_answer_bytes",
        "refused_failed",
        "refused_before_dispatch",
        "refused_reason_requested_above_ceiling",
        "refused_request_above_contract_ceiling",
        "refused_budget_source_contract",
    ):
        assert f"+{name}" in result.assertions
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R6"
    assert parsed.trigger is EnumScenarioTrigger.SCHEDULE
    assert parsed.slot_start == datetime(2026, 11, 9, 0, 0, tzinfo=UTC)
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R6")
    ]


def test_golden_chain_the_overlay_roots_of_the_host_are_put_back_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, "/host/overlay/root")
    handler, _, _ = build(tmp_path)
    handler.run()
    import os

    assert os.environ[OVERLAY_ROOTS_ENV] == "/host/overlay/root"


def test_golden_chain_a_host_overlay_never_changes_the_no_overlay_leg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host whose own overlay already raises the ceiling would admit the request; the leg ignores the host's roots."""
    host_root = tmp_path / "host-root" / "delegation_budgets"
    host_root.mkdir(parents=True)
    (host_root / "overlay.yaml").write_text(
        "execution_budgets:\n  summarization:\n    task_class_timeout_ceiling_seconds: 600\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "host-root"))
    handler, _, _ = build(tmp_path / "run")
    result = handler.run()
    assert failed_assertions(result.assertions) == [], result.detail


def test_golden_chain_manual_trigger_is_recorded_as_manual(tmp_path: Path) -> None:
    handler, rows, _ = build(tmp_path, trigger=EnumScenarioTrigger.MANUAL)
    handler.run()
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.trigger is EnumScenarioTrigger.MANUAL


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR6(
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


def test_error_chain_an_overlay_ceiling_below_the_request_refuses_the_long_run(
    tmp_path: Path,
) -> None:
    low = changed_inputs(tmp_path, overlay_ceiling_seconds=300)
    handler, rows, _ = build(tmp_path, inputs_path=low)
    result = handler.run()
    assert "-long_completed" in result.assertions
    assert "-long_ceiling_from_overlay" not in result.assertions
    assert "+refused_failed" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "long_completed" in result.detail
    assert len(rows.rows) == 1


def test_error_chain_a_rung_that_answers_inside_the_contract_ceiling_is_not_a_long_task(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["rung"]["delay_ms"] = 10000
    short = tmp_path / "short.yaml"
    short.write_text(yaml.safe_dump(raw), encoding="utf-8")
    handler, _, _ = build(tmp_path, inputs_path=short)
    result = handler.run()
    assert "-long_duration_over_contract_ceiling" in result.assertions
    assert "+long_completed" in result.assertions


def test_error_chain_a_wrong_answer_is_caught_by_the_byte_comparison(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("wrong_bytes"))
    result = handler.run()
    assert "-long_answer_bytes" in result.assertions


def test_error_chain_a_request_inside_the_contract_ceiling_is_admitted_so_the_falsifier_fails(
    tmp_path: Path,
) -> None:
    """The no-overlay leg only proves the refusal while the request is above the contract's ceiling."""
    admitted = changed_inputs(tmp_path, requested_timeout_seconds=200)
    handler, _, _ = build(tmp_path, inputs_path=admitted)
    result = handler.run()
    assert "-refused_failed" in result.assertions
    assert "-refused_reason_requested_above_ceiling" in result.assertions


def test_error_chain_unreadable_inputs_is_a_failure_and_still_writes_one_row(
    tmp_path: Path,
) -> None:
    handler, rows, events = build(tmp_path, inputs_path=tmp_path / "missing.yaml")
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_error_chain_a_ledger_append_that_fails_raises_and_emits_nothing(
    tmp_path: Path,
) -> None:
    def refuse(row: str) -> None:
        raise RuntimeError("the ledger append was rejected")

    handler, _, events = build(tmp_path, appender=refuse)
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
    handler = HandlerScenarioR6(
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


def test_the_schedule_block_renders_a_daily_user_timer() -> None:
    schedule = load_schedule(CONTRACT_PATH)
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r6_effect",
        omni_root=Path("/opt/omni"),
        home=Path("/home/lab"),
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={"ONEX_HOST_NAME": "lab-a"},
    )
    assert "OnCalendar=*-*-* 03:40:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r6_effect run" in service
    assert 'Environment="ONEX_HOST_NAME=lab-a"' in service
