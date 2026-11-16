# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R7 (OMN-23696): one run writes one STATUS row and emits one result on the bus.

The golden chain runs the scenario on the scripted in-memory transport: the two rungs are scripted, the first failing
and the second answering at a scripted cost, so no test sleeps. The real loopback backend is the production setting
and is run on the lab; no test here runs it. The error chains are named injected breaks: each makes exactly one thing
wrong and shows the scenario red for that reason, with the same input green without the break.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.delegation_readiness_probe import (
    ModelLegOutcome,
    ModelLegSpec,
    ProtocolLegTransport,
    ScriptedLegTransport,
)
from omnimarket.handlers.delegation_scenario_runtime import (
    InMemoryScenarioEventSink,
    JournalScenarioEventSink,
    render_systemd,
)
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r7_effect.handlers.handler_scenario_r7 import (
    CONTRACT_PATH,
    NODE_DIR,
    HandlerScenarioR7,
    ModelScenarioR7RunCommand,
    load_runtime,
    load_schedule,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.delegation-scenario-result.v1"
NOW = datetime(2026, 11, 9, 3, 41, 30, tzinfo=UTC)
RUNTIME = load_runtime().model_copy(update={"leg_backend": "inmemory"})
INPUTS = NODE_DIR / RUNTIME.inputs_relpath


class Rows:
    def __init__(self) -> None:
        self.rows: list[str] = []

    def __call__(self, row: str) -> None:
        self.rows.append(row)


class MislabelledReceipt(HandlerScenarioR7):
    """The product with one named wrong behaviour: the receipt names a different backend than the rung that answered."""

    def _run_leg(self, spec: ModelLegSpec) -> ModelLegOutcome:
        outcome = super()._run_leg(spec)
        return outcome.model_copy(
            update={
                "attempts": tuple(
                    attempt.model_copy(update={"backend_id": "r7-mislabelled"})
                    if attempt.accepted
                    else attempt
                    for attempt in outcome.attempts
                )
            }
        )


class WrongHostReceipt(HandlerScenarioR7):
    """The product with one named wrong behaviour: the receipt names a host that did not serve the run."""

    def _run_leg(self, spec: ModelLegSpec) -> ModelLegOutcome:
        outcome = super()._run_leg(spec)
        return outcome.model_copy(
            update={
                "attempts": tuple(
                    attempt.model_copy(update={"host": "other-host"})
                    for attempt in outcome.attempts
                )
            }
        )


class NoHostReceipt(HandlerScenarioR7):
    """The product with one named wrong behaviour: the receipt names no host at all."""

    def _run_leg(self, spec: ModelLegSpec) -> ModelLegOutcome:
        outcome = super()._run_leg(spec)
        return outcome.model_copy(
            update={
                "attempts": tuple(
                    attempt.model_copy(update={"host": None})
                    for attempt in outcome.attempts
                )
            }
        )


def build(
    tmp_path: Path,
    transport: Callable[[], ProtocolLegTransport] = ScriptedLegTransport,
    *,
    handler_class: type[HandlerScenarioR7] = HandlerScenarioR7,
    inputs_path: Path = INPUTS,
    appender: Callable[[str], None] | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
) -> tuple[HandlerScenarioR7, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    handler = handler_class(
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


def test_golden_chain(tmp_path: Path) -> None:
    handler, rows, events = build(tmp_path)
    result = handler.handle(ModelScenarioR7RunCommand())
    assert failed_assertions(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    for name in (
        "leg_completed",
        "walked_failing_then_answering",
        "failed_attempt_names_failing_rung",
        "accepted_attempt_names_answering_backend",
        "accepted_attempt_names_serving_host",
        "failed_attempt_names_serving_host",
        "accepted_attempt_cost_positive",
        "terminal_cost_equals_attempts",
        "answer_bytes",
        "accepted_attempt_cost_scripted",
    ):
        assert f"+{name}" in result.assertions
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R7"
    assert parsed.trigger is EnumScenarioTrigger.SCHEDULE
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R7")
    ]


def test_control_mislabelled_backend(tmp_path: Path) -> None:
    green_handler, _, _ = build(tmp_path)
    green = green_handler.run()
    assert failed_assertions(green.assertions) == [], green.detail
    handler, _, _ = build(tmp_path, handler_class=MislabelledReceipt)
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-accepted_attempt_names_answering_backend" in result.assertions


def test_control_wrong_host(tmp_path: Path) -> None:
    green_handler, _, _ = build(tmp_path)
    assert failed_assertions(green_handler.run().assertions) == []
    handler, _, _ = build(tmp_path, handler_class=WrongHostReceipt)
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-accepted_attempt_names_serving_host" in result.assertions
    assert "-failed_attempt_names_serving_host" in result.assertions


def test_control_no_host_on_the_receipt(tmp_path: Path) -> None:
    handler, _, _ = build(tmp_path, handler_class=NoHostReceipt)
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-accepted_attempt_names_serving_host" in result.assertions


def test_error_chain_every_rung_fails(tmp_path: Path) -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["failing_rung"] = dict(raw["failing_rung"])
    raw["answering_rung"] = {
        "backend_id": raw["answering_rung"]["backend_id"],
        "model_id": raw["answering_rung"]["model_id"],
        "behaviour": "status",
        "status_code": 503,
        "cost_class": "free",
        "tier": "local",
    }
    path = tmp_path / "both_fail.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    handler, rows, events = build(tmp_path, inputs_path=path)
    result = handler.run()
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert "-leg_completed" in result.assertions
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_error_chain_inputs_unreadable(tmp_path: Path) -> None:
    handler, rows, events = build(tmp_path, inputs_path=tmp_path / "missing.yaml")
    result = handler.run()
    assert result.assertions == ("-inputs_readable",)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR7(
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
    handler = HandlerScenarioR7(
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


def test_the_schedule_block_renders_a_six_hour_user_timer() -> None:
    schedule = load_schedule(CONTRACT_PATH)
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r7_effect",
        omni_root=Path("/opt/omni"),
        home=Path("/home/lab"),
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={"ONEX_HOST_NAME": "lab-a"},
    )
    assert "OnCalendar=*-*-* 00,06,12,18:40:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r7_effect run" in service
    assert 'Environment="ONEX_HOST_NAME=lab-a"' in service
