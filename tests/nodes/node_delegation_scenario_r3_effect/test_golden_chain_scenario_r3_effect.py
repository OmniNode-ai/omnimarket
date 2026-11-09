# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain of scenario R3 (OMN-23182): one run writes one STATUS row and emits one result on the bus.

The golden chains run the scenario against the scripted in-memory transport and against loopback stub servers called by the
real delegation effect (leg b's real paid rung is a fake effect there, so no test reaches a network). The error chains are
named injected breaks: each makes exactly one thing wrong and shows the scenario red for that reason.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
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
from omnimarket.models.model_delegation_scenario import (
    EnumScenarioConclusion,
    EnumScenarioTrigger,
    from_status_row,
)
from omnimarket.nodes.node_delegation_scenario_r3_effect.handlers.handler_scenario_r3 import (
    CONTRACT_PATH,
    NODE_DIR,
    HandlerScenarioR3,
    ModelR3Runtime,
    ModelScenarioR3RunCommand,
    load_inputs,
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
#: a stand-in value, never a real key; the tests prove it never leaves the call
KEY_VALUE = "stand-in-probe-value-0c9e51"
ENV = {RUNTIME.openrouter_probe_key_env: KEY_VALUE}


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
            paid = rung.cost_class == "paid" and rung.behaviour == "answer"
            if self.brk == "paid_rung_down" and paid:
                return result.model_copy(
                    update={
                        "success": False,
                        "content": "",
                        "failure_class": EnumDelegationFailureClass.MODEL_UNAVAILABLE,
                        "http_status": 503,
                    }
                )
            if self.brk == "free_rung_answers" and rung.behaviour == "status":
                return result.model_copy(
                    update={
                        "success": True,
                        "content": self._answers[request.endpoint_ref],
                        "failure_class": None,
                        "http_status": 200,
                        "error_message": None,
                    }
                )
            if self.brk == "free_rung_untyped" and rung.behaviour == "status":
                return result.model_copy(
                    update={"failure_class": EnumDelegationFailureClass.UNKNOWN}
                )
            if self.brk == "wrong_bytes" and paid:
                return result.model_copy(
                    update={
                        "content": "### ANSWER\n"
                        + "A different but long enough reply about something else entirely. "
                        * 6
                    }
                )
            if self.brk == "paid_cost_zero" and paid:
                return result.model_copy(
                    update={
                        "actual_cost_usd": Decimal("0"),
                        "usage_source": EnumUsageSource.UNKNOWN,
                    }
                )
            return result

        return call


def build(
    tmp_path: Path,
    transport: Callable[[], ProtocolLegTransport] = ScriptedLegTransport,
    *,
    runtime: ModelR3Runtime = RUNTIME,
    environment: Mapping[str, str] = ENV,
    inputs_path: Path = INPUTS,
    appender: Callable[[str], None] | None = None,
    sink: InMemoryScenarioEventSink | None = None,
    trigger: EnumScenarioTrigger = EnumScenarioTrigger.SCHEDULE,
    paid_rung_effect: Callable[
        [ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult
    ]
    | None = None,
) -> tuple[HandlerScenarioR3, Rows, InMemoryScenarioEventSink]:
    rows = Rows()
    events = sink or InMemoryScenarioEventSink()
    handler = HandlerScenarioR3(
        runtime,
        home=tmp_path,
        environment=environment,
        appender=appender or rows,
        sink=events,
        terminal_topic=TERMINAL,
        transport_factory=transport,
        trigger=trigger,
        clock=lambda: NOW,
        inputs_path=inputs_path,
        paid_rung_effect=paid_rung_effect,
    )
    return handler, rows, events


def failed_assertions(assertions: tuple[str, ...]) -> list[str]:
    return [item for item in assertions if item.startswith("-")]


def edited_inputs(tmp_path: Path, edit: Callable[[dict[str, object]], None]) -> Path:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    edit(raw)
    path = tmp_path / "edited.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


class FakeOpenRouter:
    """Stands in for the paid model: records the request it is given and answers like a measured paid call."""

    def __init__(self, content: str, cost: str = "0.00001") -> None:
        self.content = content
        self.cost = Decimal(cost)
        self.requests: list[ModelLlmDelegationCallRequest] = []

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.requests.append(request)
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=self.content,
            tokens_in=30,
            tokens_out=16,
            latency_ms=5,
            actual_cost_usd=self.cost,
            savings_usd=Decimal("0"),
            http_status=200,
            usage_source=EnumUsageSource.MEASURED
            if self.cost > 0
            else EnumUsageSource.UNKNOWN,
        )


def test_golden_chain_inmemory_every_assertion_held(tmp_path: Path) -> None:
    handler, rows, events = build(tmp_path)

    result = handler.handle(ModelScenarioR3RunCommand())

    assert failed_assertions(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    for leg in ("leg_a", "leg_b"):
        for check in (
            "free_rung_retries",
            "one_move",
            "completed",
            "paid_rung_accepted",
            "size_band",
        ):
            assert f"+{leg}_{check}" in result.assertions
    assert "+leg_a_answer_bytes" in result.assertions
    assert "+leg_b_usage_measured" in result.assertions
    assert "+leg_b_cost_positive" in result.assertions
    assert "+every_down_returned_to_caller" in result.assertions
    assert len(rows.rows) == 1
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.scenario_id == "R3"
    assert parsed.trigger is EnumScenarioTrigger.SCHEDULE
    assert parsed.slot_start == datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
    assert f" {RUNTIME.lane} " in rows.rows[0] or RUNTIME.lane in rows.rows[0]
    assert [(topic, payload["scenario_id"]) for topic, payload in events.events] == [
        (TERMINAL, "R3")
    ]


def test_golden_chain_loopback_stubs_with_the_real_paid_rung_rewritten_to_openrouter(
    tmp_path: Path,
) -> None:
    """Production's delegation effect calls the loopback stubs; the real paid rung reaches the effect seam rewritten."""
    answer = load_inputs(INPUTS).answer
    paid = FakeOpenRouter(answer)
    handler, rows, events = build(tmp_path, LoopbackStubServers, paid_rung_effect=paid)

    result = handler.run()

    assert failed_assertions(result.assertions) == [], result.detail
    assert result.conclusion is EnumScenarioConclusion.SUCCESS
    assert "+leg_b_usage_measured" in result.assertions
    assert "+leg_b_cost_positive" in result.assertions
    assert len(paid.requests) == 1
    sent = paid.requests[0]
    assert sent.endpoint_ref == RUNTIME.openrouter_base_url
    assert sent.model_id == RUNTIME.openrouter_model_id
    assert sent.max_tokens == RUNTIME.openrouter_max_tokens == 16
    assert sent.provider == "openrouter"
    assert sent.extra_headers["Authorization"] == f"Bearer {KEY_VALUE}"
    assert len(rows.rows) == 1
    assert len(events.events) == 1


def test_the_probe_key_value_is_in_no_row_event_assertion_or_detail(
    tmp_path: Path,
) -> None:
    paid = FakeOpenRouter(load_inputs(INPUTS).answer)
    handler, rows, events = build(tmp_path, LoopbackStubServers, paid_rung_effect=paid)
    result = handler.run()
    everything = json.dumps(
        [
            rows.rows,
            [payload for _, payload in events.events],
            result.model_dump(mode="json"),
        ]
    )
    assert KEY_VALUE not in everything
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert KEY_VALUE.encode() not in path.read_bytes(), path


def test_golden_chain_manual_trigger_is_recorded_as_manual(tmp_path: Path) -> None:
    handler, rows, _ = build(tmp_path, trigger=EnumScenarioTrigger.MANUAL)
    handler.run()
    parsed = from_status_row(rows.rows[0])
    assert parsed is not None
    assert parsed.trigger is EnumScenarioTrigger.MANUAL


def test_golden_chain_event_is_journalled(tmp_path: Path) -> None:
    journal = tmp_path / "events.jsonl"
    handler = HandlerScenarioR3(
        RUNTIME,
        home=tmp_path,
        environment=ENV,
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


def test_error_chain_a_paid_rung_that_is_down_too_ends_returned_to_caller_and_turns_leg_a_red(
    tmp_path: Path,
) -> None:
    """Falsifier control: both rungs 503. The run ends returned_to_caller and the scenario is red for that break."""
    handler, rows, _ = build(tmp_path, lambda: Breakable("paid_rung_down"))
    result = handler.run()
    assert "-leg_a_completed" in result.assertions
    assert "-leg_a_paid_rung_accepted" in result.assertions
    assert "-leg_b_completed" in result.assertions
    assert "returned_to_caller" in result.detail
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert len(rows.rows) == 1


def test_control_both_rungs_503_ends_failed_returned_to_caller_with_every_attempt_typed(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path)
    result = handler.run()
    for name in (
        "every_down_failed",
        "every_down_returned_to_caller",
        "every_down_reason_in_closed_list",
        "every_down_typed",
    ):
        assert f"+{name}" in result.assertions


def test_error_chain_the_control_ladder_answering_turns_every_down_red(
    tmp_path: Path,
) -> None:
    def edit(raw: dict[str, object]) -> None:
        raw["all_down_leg"]["rungs"][1]["behaviour"] = "answer"  # type: ignore[index]
        raw["all_down_leg"]["rungs"][1].pop("status_code")  # type: ignore[index]

    handler, _, _ = build(tmp_path, inputs_path=edited_inputs(tmp_path, edit))
    result = handler.run()
    assert "-every_down_failed" in result.assertions
    assert "-every_down_returned_to_caller" in result.assertions
    assert "+leg_a_completed" in result.assertions


def test_error_chain_a_free_rung_that_answers_is_not_a_forced_escalation(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("free_rung_answers"))
    result = handler.run()
    assert "-leg_a_free_rung_retries" in result.assertions
    assert "-leg_a_one_move" in result.assertions
    assert "-leg_a_paid_rung_accepted" in result.assertions


def test_error_chain_a_free_rung_typed_unknown_turns_the_retry_assertion_red(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("free_rung_untyped"))
    result = handler.run()
    assert "-leg_a_free_rung_retries" in result.assertions
    assert "-leg_b_free_rung_retries" in result.assertions


def test_error_chain_a_wrong_answer_from_the_paid_rung_is_caught_by_the_byte_comparison(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("wrong_bytes"))
    result = handler.run()
    assert "-leg_a_answer_bytes" in result.assertions
    assert "+leg_a_completed" in result.assertions


def test_error_chain_a_paid_rung_whose_measured_cost_is_zero_turns_leg_b_red(
    tmp_path: Path,
) -> None:
    handler, _, _ = build(tmp_path, lambda: Breakable("paid_cost_zero"))
    result = handler.run()
    assert "-leg_b_usage_measured" in result.assertions
    assert "-leg_b_cost_positive" in result.assertions


def test_error_chain_the_real_paid_rung_reporting_no_cost_turns_leg_b_red(
    tmp_path: Path,
) -> None:
    paid = FakeOpenRouter(load_inputs(INPUTS).answer, cost="0")
    handler, _, _ = build(tmp_path, LoopbackStubServers, paid_rung_effect=paid)
    result = handler.run()
    assert "-leg_b_cost_positive" in result.assertions
    assert "-leg_b_usage_measured" in result.assertions
    assert "+leg_a_completed" in result.assertions


def test_error_chain_a_ladder_whose_paid_rung_is_not_in_a_paid_tier_turns_the_paid_assertion_red(
    tmp_path: Path,
) -> None:
    def edit(raw: dict[str, object]) -> None:
        raw["paid_tiers"] = ["claude"]

    handler, _, _ = build(tmp_path, inputs_path=edited_inputs(tmp_path, edit))
    result = handler.run()
    assert "-leg_a_paid_rung_accepted" in result.assertions
    assert "-leg_b_paid_rung_accepted" in result.assertions


def test_error_chain_a_small_input_that_is_not_in_the_large_band_turns_the_size_assertion_red(
    tmp_path: Path,
) -> None:
    def edit(raw: dict[str, object]) -> None:
        raw["prompt"] = "Say hello."

    handler, _, _ = build(tmp_path, inputs_path=edited_inputs(tmp_path, edit))
    result = handler.run()
    assert "-leg_a_size_band" in result.assertions


def test_error_chain_an_empty_probe_key_with_leg_b_enabled_is_red_never_skipped(
    tmp_path: Path,
) -> None:
    handler, rows, _ = build(tmp_path, environment={})
    result = handler.run()
    assert "-leg_b_probe_key_present" in result.assertions
    assert not any("leg_b_usage" in a for a in result.assertions)
    assert result.conclusion is EnumScenarioConclusion.FAILURE
    assert RUNTIME.openrouter_probe_key_env in result.detail
    assert len(rows.rows) == 1


def test_error_chain_a_whitespace_only_probe_key_is_empty(tmp_path: Path) -> None:
    handler, _, _ = build(
        tmp_path, environment={RUNTIME.openrouter_probe_key_env: "   "}
    )
    assert "-leg_b_probe_key_present" in handler.run().assertions


def test_error_chain_leg_b_switched_off_is_red_never_skipped(tmp_path: Path) -> None:
    off = RUNTIME.model_copy(update={"leg_b_enabled": False})
    handler, _, _ = build(tmp_path, runtime=off)
    result = handler.run()
    assert "-leg_b_enabled" in result.assertions
    assert result.conclusion is EnumScenarioConclusion.FAILURE


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
    handler = HandlerScenarioR3(
        RUNTIME,
        home=tmp_path,
        environment=ENV,
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
    lab_home = Path("/home/lab")
    service, timer = render_systemd(
        schedule,
        node_module="omnimarket.nodes.node_delegation_scenario_r3_effect",
        omni_root=Path("/opt/omni"),
        home=lab_home,
        python=Path("/opt/omni/omnimarket/.venv/bin/python"),
        environment={
            "ONEX_HOST_NAME": "lab-a",
            RUNTIME.openrouter_probe_key_env: KEY_VALUE,
        },
    )
    assert "OnCalendar=*-*-* 00/6:25:00 UTC" in timer
    assert "-m omnimarket.nodes.node_delegation_scenario_r3_effect run" in service
    assert 'Environment="ONEX_HOST_NAME=lab-a"' in service
    assert KEY_VALUE not in service
    assert schedule.environment_file is not None
    assert f"EnvironmentFile=-{lab_home / schedule.environment_file}" in service
