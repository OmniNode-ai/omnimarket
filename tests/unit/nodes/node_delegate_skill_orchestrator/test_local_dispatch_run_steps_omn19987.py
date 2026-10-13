# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: the local delegate path numbers each step of a run and emits it.

The Workflow page shows one run's ordered path. The order is a number the
orchestrator assigns from one counter per run and carries on each step event;
nothing downstream may derive it from arrival or from a clock. One local
dispatch must leave its steps in the local store's ``run_trace`` table and on
the declared topic, numbered 1..n in the order the orchestrator took them.

Failure modes:

* no step is emitted at all (the page has nothing to show);
* a run's numbers start above 1, skip, or repeat;
* a second run continues the first run's counter;
* an escalated run shows one provider call, or no second routing decision;
* a provider call that failed is recorded as succeeded, or without its cause;
* a failed run's terminal names itself, or nothing, as the failing step, instead
  of the provider call or quality gate that failed it (WF-2);
* a run that succeeded after an earlier failure names a failing step;
* the stored steps and the published steps disagree (two sources of one fact);
* a step carries no tenant, or a tenant other than the delegation row's;
* a step publish, or a step store write, that raises breaks the delegation or
  changes its result;
* the steps are published on a topic the contract does not declare;
* shuffling the emitted events before folding them gives another order.
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_delegate_skill_orchestrator import run_step_recorder
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_store import (
    apply_run_step,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_escalation_omn13849 import (
    _TIER_BACKEND_ID,
    _install_ladder,
    _PerTierEffect,
)

pytestmark = pytest.mark.unit

STEP_TOPIC = "onex.evt.omnimarket.delegation-run-step.v1"
_GOOD = _PerTierEffect._GOOD_RESEARCH


class _RecordingPublisher:
    """Stands where the emit effect stands; keeps what it was asked to publish."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        self.published.append(
            {
                "topic": topic,
                "event_type": event_type,
                "payload": dict(payload),
                "event_id": event_id,
                "correlation_id": correlation_id,
                "partition_key": partition_key,
            }
        )
        return True


def _port(
    tmp_path: Path, effect: Any, publisher: Any | None = None
) -> tuple[LocalDelegationDispatchPort, _RecordingPublisher, Path]:
    recording = publisher or _RecordingPublisher()
    db_path = tmp_path / "d.sqlite"
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=db_path,
        effect_process_boundary=False,
        run_step_publisher=recording,
    )
    return port, recording, db_path


def _dispatch(
    port: LocalDelegationDispatchPort, correlation_id: UUID
) -> dict[str, Any]:
    result: dict[str, Any] = asyncio.run(
        port.dispatch(
            prompt="explain the tradeoff",
            task_type="research",
            correlation_id=correlation_id,
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )
    return result


def _stored(db_path: Path, correlation_id: UUID) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM run_trace WHERE correlation_id = ? ORDER BY step_sequence",
            (str(correlation_id),),
        ).fetchall()
    finally:
        conn.close()


def _steps(
    publisher: _RecordingPublisher, correlation_id: UUID
) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in publisher.published
        if event["topic"] == STEP_TOPIC
        and event["payload"]["correlation_id"] == str(correlation_id)
    ]


def _kinds(steps: list[Any]) -> list[str]:
    return [str(step["step_kind"]) for step in steps]


def _failing(failure_class: EnumDelegationFailureClass, *, on: frozenset[str]) -> Any:
    def effect(request: ModelLlmDelegationCallRequest) -> ModelLlmDelegationCallResult:
        if request.model_tier in on:
            return ModelLlmDelegationCallResult(
                request_id=request.request_id,
                success=False,
                failure_class=failure_class,
                error_message=f"{failure_class.value} on {request.model_tier}",
            )
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=_GOOD,
            tokens_in=11,
            tokens_out=22,
            latency_ms=5,
            actual_cost_usd=Decimal("0.010"),
            savings_usd=Decimal("0"),
        )

    return effect


def test_a_run_that_passes_first_time_emits_its_five_steps_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    run = uuid4()
    assert _dispatch(port, run)["status"] == "completed"

    steps = _steps(publisher, run)
    assert [step["step_sequence"] for step in steps] == [1, 2, 3, 4, 5]
    assert _kinds(steps) == [
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]
    assert {step["outcome"] for step in steps} == {"succeeded"}
    assert steps[1]["backend_id"] == _TIER_BACKEND_ID["local"]
    assert steps[2]["backend_id"] == _TIER_BACKEND_ID["local"]
    assert steps[2]["attempt_number"] == 1
    assert steps[-1].get("failing_step_sequence") is None
    for step in steps:
        ModelDelegationRunStep.model_validate(step)


def test_the_stored_steps_are_the_published_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    )
    run = uuid4()
    _dispatch(port, run)

    published = _steps(publisher, run)
    stored = _stored(db_path, run)
    assert len(stored) == len(published) >= 5
    for row, step in zip(stored, published, strict=True):
        assert row["step_sequence"] == step["step_sequence"]
        assert row["step_kind"] == step["step_kind"]
        assert row["outcome"] == step["outcome"]
        assert row["tenant_id"] == step["tenant_id"]
        assert row["backend_id"] == step.get("backend_id")
        assert row["failure_cause"] == step.get("failure_cause")


def test_an_escalated_run_shows_each_routing_decision_call_and_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    )
    run = uuid4()
    result = _dispatch(port, run)
    assert result["status"] == "completed"
    assert result["escalation_count"] == 1

    steps = _steps(publisher, run)
    assert [step["step_sequence"] for step in steps] == list(range(1, len(steps) + 1))
    assert _kinds(steps) == [
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]
    calls = [step for step in steps if step["step_kind"] == "provider_call"]
    assert [call["backend_id"] for call in calls] == [
        _TIER_BACKEND_ID["local"],
        _TIER_BACKEND_ID["cheap_cloud"],
    ]
    assert [call["attempt_number"] for call in calls] == [1, 2]
    gates = [step for step in steps if step["step_kind"] == "quality_gate"]
    assert [gate["outcome"] for gate in gates] == ["failed", "succeeded"]
    assert gates[0]["failure_cause"]
    # The run succeeded, so its terminal blames no step for a failure.
    assert steps[-1]["outcome"] == "succeeded"
    assert steps[-1].get("failing_step_sequence") is None
    second_route = steps[4]
    assert second_route["backend_id"] == _TIER_BACKEND_ID["cheap_cloud"]
    assert second_route["reason"]


def test_a_timed_out_run_blames_the_provider_call_not_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=0)
    port, publisher, db_path = _port(
        tmp_path,
        _failing(EnumDelegationFailureClass.TIMEOUT, on=frozenset({"local"})),
    )
    run = uuid4()
    assert _dispatch(port, run)["status"] == "failed"

    steps = _steps(publisher, run)
    assert _kinds(steps) == ["request", "routing_decision", "provider_call", "terminal"]
    call, terminal = steps[2], steps[3]
    assert call["outcome"] == "failed"
    assert call["failure_cause"] == "timeout"
    assert terminal["outcome"] == "failed"
    assert terminal["failure_cause"] == "timeout"
    assert terminal["failing_step_sequence"] == call["step_sequence"] == 3
    stored = _stored(db_path, run)
    assert stored[-1]["failing_step_sequence"] == 3


def test_a_run_whose_every_rung_fails_the_gate_blames_the_last_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(tmp_path, _PerTierEffect(pass_tiers=frozenset()))
    run = uuid4()
    assert _dispatch(port, run)["status"] == "failed"

    steps = _steps(publisher, run)
    terminal = steps[-1]
    gates = [step for step in steps if step["step_kind"] == "quality_gate"]
    assert len(gates) >= 2
    assert {gate["outcome"] for gate in gates} == {"failed"}
    assert terminal["step_kind"] == "terminal"
    assert terminal["outcome"] == "failed"
    assert terminal["failing_step_sequence"] == gates[-1]["step_sequence"]


def test_a_failed_call_followed_by_success_leaves_the_failure_on_its_own_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(
        tmp_path,
        _failing(EnumDelegationFailureClass.MODEL_UNAVAILABLE, on=frozenset({"local"})),
    )
    run = uuid4()
    assert _dispatch(port, run)["status"] == "completed"

    steps = _steps(publisher, run)
    calls = [step for step in steps if step["step_kind"] == "provider_call"]
    assert [(c["outcome"], c.get("failure_cause")) for c in calls] == [
        ("failed", "model_unavailable"),
        ("succeeded", None),
    ]
    assert steps[-1]["outcome"] == "succeeded"
    assert steps[-1].get("failing_step_sequence") is None


def test_each_run_counts_its_own_steps_from_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    first, second = uuid4(), uuid4()
    _dispatch(port, first)
    _dispatch(port, second)
    for run in (first, second):
        assert [s["step_sequence"] for s in _steps(publisher, run)] == [1, 2, 3, 4, 5]
        assert [row["step_sequence"] for row in _stored(db_path, run)] == [
            1,
            2,
            3,
            4,
            5,
        ]


def test_every_step_carries_the_delegation_rows_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    run = uuid4()
    _dispatch(port, run)
    conn = sqlite3.connect(str(db_path))
    try:
        (tenant,) = conn.execute(
            "SELECT tenant_id FROM delegation_events WHERE correlation_id = ?",
            (str(run),),
        ).fetchone()
    finally:
        conn.close()
    assert tenant
    assert {step["tenant_id"] for step in _steps(publisher, run)} == {tenant}


def test_steps_are_published_on_the_declared_topic_keyed_by_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    run = uuid4()
    _dispatch(port, run)
    contract = yaml.safe_load(
        (Path(run_step_recorder.__file__).resolve().parent / "contract.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert contract["runtime_dispatch"]["run_step_event"] == STEP_TOPIC
    assert STEP_TOPIC in contract["event_bus"]["publish_topics"]
    events = [e for e in publisher.published if e["topic"] == STEP_TOPIC]
    assert len(events) == 5
    assert {e["partition_key"] for e in events} == {str(run)}
    assert {e["correlation_id"] for e in events} == {str(run)}
    assert len({e["event_id"] for e in events}) == 5


class _RaisingPublisher:
    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        raise RuntimeError("bus unavailable")


def test_a_publisher_that_raises_changes_nothing_about_the_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    effect = _PerTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    port = LocalDelegationDispatchPort(
        effect_handler=effect,
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        run_step_publisher=_RaisingPublisher(),
    )
    run = uuid4()
    result = _dispatch(port, run)
    assert result["status"] == "completed"
    assert result["escalation_count"] == 1
    assert effect.calls == ["local", "cheap_cloud"]
    # The local store still holds the whole run: the two deliveries are independent.
    assert [
        row["step_sequence"] for row in _stored(tmp_path / "d.sqlite", run)
    ] == list(range(1, 9))


def test_a_step_store_write_that_raises_changes_nothing_about_the_delegation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)

    def boom(*_: object, **__: object) -> bool:
        raise RuntimeError("step store unavailable")

    monkeypatch.setattr(run_step_recorder, "apply_run_step", boom)
    port, publisher, db_path = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"local"}))
    )
    run = uuid4()
    assert _dispatch(port, run)["status"] == "completed"
    # Still published, still numbered, and the delegation row still lands.
    assert [s["step_sequence"] for s in _steps(publisher, run)] == [1, 2, 3, 4, 5]
    conn = sqlite3.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM delegation_events").fetchone() == (1,)
    finally:
        conn.close()


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_folding_the_emitted_events_shuffled_gives_the_emitted_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int
) -> None:
    _install_ladder(monkeypatch, max_escalations=2)
    port, publisher, _ = _port(
        tmp_path, _PerTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    )
    run = uuid4()
    _dispatch(port, run)
    emitted = _steps(publisher, run)
    delivered = list(emitted)
    random.Random(seed).shuffle(delivered)
    assert delivered != emitted

    replay = SqliteDatabaseAdapter(tmp_path / "replay.sqlite")
    fold = HandlerProjectionRunTrace()
    for event in delivered:
        apply_run_step(
            fold.handle(ModelDelegationRunStep.model_validate(event)), replay
        )
    rows = replay.query(
        "run_trace", {"correlation_id": str(run)}, order_by="step_sequence"
    )
    assert [row["step_kind"] for row in rows] == _kinds(emitted)
