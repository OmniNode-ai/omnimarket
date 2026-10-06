# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command-keyed terminal arbitration and reaper liveness (OMN-19441)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.cli.delegate_terminal_resolver import resolve_delegate_terminal
from omnibase_infra.cli.model_delegate_terminal import ModelDelegateTerminal
from omnibase_infra.runtime.dispatch_envelope_context import bind_dispatch_envelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    CLAIMS_TABLE,
    DelegationClaimPort,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit
CONTRACT = Path("src/omnimarket/nodes/node_delegate_skill_orchestrator/contract.yaml")


def _setup(correlation_id=None, database=None):
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegation_reaper import (
        HandlerDelegationReaper,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_reap_context import (
        ModelDelegationReapContext,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
        ModelDelegationReaperConfig,
    )

    db = database if database is not None else InmemoryDatabaseAdapter()
    port = DelegationClaimPort(database=db)
    delivery_id = uuid4()
    context = ModelDelegationReapContext(
        correlation_id=correlation_id or uuid4(),
        task_type="test",
        tenant_id="omninode",
        ticket_id="OMN-19441",
        caller_lane="test",
        session_id=str(uuid4()),
        provenance=None,
        deadline_at=datetime.now(UTC) + timedelta(seconds=240 + 60 + 60),
    )
    assert port.claim(
        delivery_id=delivery_id,
        correlation_id=context.correlation_id,
        reap_context=context,
    ).won
    # A one second scan interval keeps the throttle out of the way of tests that
    # drive the clock by whole seconds; the throttle has its own test below.
    config = ModelDelegationReaperConfig(
        grace_seconds=60, max_reaps_per_tick=25, scan_interval_seconds=1
    )
    return (
        db,
        port,
        HandlerDelegationReaper(port=port, config=config),
        delivery_id,
        context,
    )


def _tick(now):
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        correlation_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        scheduler_id="reaper-test",
        tick_interval_ms=1000,
    )


def _record(terminal):
    return {"cls": type(terminal).__name__, "data": terminal.model_dump(mode="json")}


def _row(db, key):
    return db.query(CLAIMS_TABLE, {"delivery_id": key})[0]


def _late_rows(db, delivery_id):
    return [
        row
        for row in db.query(CLAIMS_TABLE)
        if str(row["delivery_id"]).startswith(f"late:{delivery_id}:")
    ]


async def test_a_claimed_command_with_no_terminal_at_its_budget_plus_60_seconds_gets_exactly_one_no_terminal_terminal():
    db, _, reaper, delivery_id, ctx = _setup()
    assert await reaper.handle(_tick(ctx.deadline_at - timedelta(seconds=1))) is None
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    assert len(output.events) == 1
    terminal = output.events[0]
    assert isinstance(terminal, ModelDelegateSkillFailed)
    assert terminal.status == "failed"
    assert terminal.terminal_failure_cause.value == "no_terminal"
    assert terminal.correlation_id == ctx.correlation_id
    assert terminal.command_id == delivery_id
    assert terminal.task_type == ctx.task_type
    assert terminal.tenant_id == ctx.tenant_id
    assert terminal.ticket_id == ctx.ticket_id
    assert terminal.session_id == ctx.session_id
    claimed_at = datetime.fromisoformat(_row(db, f"reap:{delivery_id}")["claimed_at"])
    assert terminal.execution_duration_ms == int(
        (ctx.deadline_at - claimed_at).total_seconds() * 1000
    )
    assert json.loads(_row(db, f"slot:{delivery_id}")["terminal_json"]) == _record(
        terminal
    )
    for seconds in (2, 100):
        assert (
            await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=seconds)))
            is None
        )


async def test_a_real_terminal_arriving_after_the_reaper_is_kept_as_attempt_evidence_and_publishes_no_second_terminal():
    db, port, reaper, delivery_id, ctx = _setup()
    started = asyncio.Event()
    finish = asyncio.Event()

    class Dispatch:
        async def dispatch(self, **kwargs):
            started.set()
            await finish.wait()
            return {
                "status": "completed",
                "content": "proof",
                "quality_gate_passed": True,
            }

    handler = HandlerDelegateSkill(dispatch_port=Dispatch(), idempotency_port=port)
    request = ModelDelegateSkillRequest(
        prompt="test",
        task_type="test",
        source="claude-code",
        correlation_id=ctx.correlation_id,
    )
    delivery = ModelEventEnvelope[object](
        envelope_id=delivery_id,
        payload={},
        correlation_id=ctx.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill",
        source_tool="reaper-test",
    )
    with bind_dispatch_envelope(delivery):
        task = asyncio.create_task(handler.handle(request))
    await asyncio.wait_for(started.wait(), timeout=5)
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    held = _row(db, f"slot:{delivery_id}")["terminal_json"]
    finish.set()
    assert await task is None
    assert _row(db, f"slot:{delivery_id}")["terminal_json"] == held
    (late,) = _late_rows(db, delivery_id)
    evidence = json.loads(late["terminal_json"])
    assert evidence["cls"] == "ModelDelegateSkillCompleted"
    assert evidence["data"]["status"] == "completed"


@pytest.mark.parametrize("slot_state", ["reaped", "committed", "not_written"])
async def test_missing_slot_verdict_keeps_attempt_evidence_without_publishing(
    monkeypatch, slot_state
):
    db, port, reaper, delivery_id, ctx = _setup()
    published = []
    upsert = db.upsert_returning

    def missing_verdict(table, key, row, **kwargs):
        if row["delivery_id"] == f"slot:{delivery_id}":
            if slot_state != "not_written":
                upsert(table, key, row, **kwargs)
            return []
        return upsert(table, key, row, **kwargs)

    started = asyncio.Event()
    finish = asyncio.Event()

    class Dispatch:
        async def dispatch(self, **kwargs):
            started.set()
            await finish.wait()
            return {
                "status": "completed",
                "content": "attempt with an unacknowledged slot write",
                "quality_gate_passed": True,
            }

    handler = HandlerDelegateSkill(dispatch_port=Dispatch(), idempotency_port=port)
    request = ModelDelegateSkillRequest(
        prompt="test",
        task_type="test",
        source="claude-code",
        correlation_id=ctx.correlation_id,
    )
    delivery = ModelEventEnvelope[object](
        envelope_id=delivery_id,
        payload={},
        correlation_id=ctx.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type="omnimarket.delegate-skill",
        source_tool="reaper-test",
    )
    with bind_dispatch_envelope(delivery):
        task = asyncio.create_task(handler.handle(request))
    await asyncio.wait_for(started.wait(), timeout=5)
    if slot_state == "reaped":
        output = await reaper.handle(_tick(ctx.deadline_at))
        published.extend(output.events)
    monkeypatch.setattr(db, "upsert_returning", missing_verdict)
    finish.set()
    assert await task is None

    (late,) = _late_rows(db, delivery_id)
    evidence = json.loads(late["terminal_json"])
    assert evidence["cls"] == "ModelDelegateSkillCompleted"
    assert evidence["data"]["response"] == "attempt with an unacknowledged slot write"
    assert _row(db, str(delivery_id))["terminal_json"] == (
        json.dumps(_record(published[0])) if published else ""
    )
    monkeypatch.setattr(db, "upsert_returning", upsert)
    output = await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=1)))
    if output is not None:
        published.extend(output.events)
    assert len(published) == 1
    terminal = published[0]
    if slot_state == "committed":
        assert isinstance(terminal, ModelDelegateSkillCompleted)
    else:
        assert terminal.terminal_failure_cause.value == "no_terminal"
    assert await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=2))) is None


@pytest.mark.parametrize("status", ["completed", "failed", "timeout"])
async def test_the_reaper_never_reaps_a_command_that_already_holds_a_terminal_including_a_handler_timeout_terminal(
    status,
):
    db, port, reaper, delivery_id, ctx = _setup()
    cls = (
        ModelDelegateSkillCompleted
        if status == "completed"
        else ModelDelegateSkillFailed
    )
    terminal = cls(
        status=status,
        correlation_id=ctx.correlation_id,
        task_type=ctx.task_type,
        terminal_failure_cause="timeout" if status == "timeout" else None,
    )
    assert port.record_terminal(delivery_id=delivery_id, terminal=_record(terminal)).won
    held = _row(db, f"slot:{delivery_id}")["terminal_json"]
    assert await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=100))) is None
    assert _row(db, f"slot:{delivery_id}")["terminal_json"] == held


async def test_shared_correlation_commands_each_hold_a_terminal():
    db, port, reaper, first, ctx = _setup()
    second = uuid4()
    port.claim(delivery_id=second, correlation_id=ctx.correlation_id, reap_context=ctx)
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    assert len(output.events) == 2
    assert all(event.correlation_id == ctx.correlation_id for event in output.events)
    assert {event.command_id for event in output.events} == {first, second}
    assert _row(db, f"slot:{first}")
    assert _row(db, f"slot:{second}")


async def test_replay_keeps_delivery_id_and_first_deadline():
    db, port, reaper, delivery_id, ctx = _setup()
    later = ctx.model_copy(update={"deadline_at": ctx.deadline_at + timedelta(hours=1)})
    assert not port.claim(
        delivery_id=delivery_id, correlation_id=ctx.correlation_id, reap_context=later
    ).won
    assert (
        json.loads(_row(db, f"reap:{delivery_id}")["terminal_json"])["deadline_at"]
        == ctx.model_dump(mode="json")["deadline_at"]
    )
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    assert len(output.events) == 1
    served = port.claim(
        delivery_id=delivery_id, correlation_id=ctx.correlation_id, reap_context=later
    )
    assert not served.won
    assert served.served_terminal == _record(output.events[0])
    assert await reaper.handle(_tick(later.deadline_at)) is None


async def test_legacy_claim_is_never_reaped():
    db, port, reaper, _, ctx = _setup()
    legacy = uuid4()
    port.claim(delivery_id=legacy, correlation_id=uuid4())
    await reaper.handle(_tick(ctx.deadline_at + timedelta(days=1)))
    assert _row(db, str(legacy))["terminal_json"] == ""
    assert not db.query(CLAIMS_TABLE, {"delivery_id": f"slot:{legacy}"})


async def test_heal_republishes_slot_and_redelivery_serves_it_before_copy():
    db, port, reaper, delivery_id, ctx = _setup()
    terminal = ModelDelegateSkillCompleted(
        correlation_id=ctx.correlation_id, task_type=ctx.task_type
    )
    db.upsert(
        CLAIMS_TABLE,
        "delivery_id",
        {
            "delivery_id": f"slot:{delivery_id}",
            "correlation_id": str(ctx.correlation_id),
            "claimed_at": datetime.now(UTC).isoformat(),
            "terminal_json": json.dumps(_record(terminal)),
        },
    )
    assert port.claim(
        delivery_id=delivery_id, correlation_id=ctx.correlation_id
    ).served_terminal == _record(terminal)
    assert _row(db, str(delivery_id))["terminal_json"] == ""
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    assert output.events == (terminal,)
    assert json.loads(_row(db, str(delivery_id))["terminal_json"]) == _record(terminal)
    assert await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=1))) is None


async def test_contract_routes_tick_and_declares_reaper_cause_and_configuration():
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
        load_delegation_reaper_config,
    )

    _, _, reaper, _, ctx = _setup()
    output = await reaper.handle(_tick(ctx.deadline_at))
    raw = yaml.safe_load(CONTRACT.read_text())
    assert (
        output.events[0].terminal_failure_cause.value
        in raw["outputs"]["terminal_failure_cause"]["enum"]
    )
    route = next(
        r
        for r in raw["handler_routing"]["handlers"]
        if r["operation"] == "delegate-skill.reap_scheduled_run"
    )
    assert route["handler"]["name"] == "HandlerDelegationReaper"
    assert route["event_type"] == "platform.runtime-tick"
    assert (
        route["event_model"]
        == "omnibase_infra.runtime.models.model_runtime_tick.ModelRuntimeTick"
    )
    assert {s["operation"] for s in raw["input_subscriptions"]} == {
        "delegate-skill.orchestrate",
        "delegate-skill.reap_scheduled_run",
        "delegate-skill.recover_completed",
        "delegate-skill.recover_failed",
    }
    config = load_delegation_reaper_config()
    assert config.grace_seconds == raw["delegation_reaper"]["grace_seconds"] == 60
    assert (
        config.max_reaps_per_tick
        == raw["delegation_reaper"]["max_reaps_per_tick"]
        == 25
    )


async def test_no_terminal_round_trip_projection_and_pinned_delegate_cli():
    _, _, reaper, _, ctx = _setup()
    output = await reaper.handle(_tick(ctx.deadline_at))
    payload = output.events[0].model_dump(mode="json")
    decoded = ModelDelegateTerminal.model_validate(payload)
    assert decoded.terminal_failure_cause == "no_terminal"
    assert resolve_delegate_terminal(payload) == decoded
    assert (
        resolve_delegate_terminal({"envelope_id": str(uuid4()), "payload": payload})
        == decoded
    )
    event = {**payload, "_event_type": "onex.evt.omnimarket.delegate-skill-failed.v1"}
    db = InmemoryDatabaseAdapter()
    projection = HandlerProjectionDelegation()
    projection.handle({**event, "_db": db})
    row = db.query(TABLE)[0]
    assert row["terminal_failure_cause"] == "no_terminal"
    assert row["operational_outcome"] == "timeout"
    success = ModelDelegateSkillCompleted(
        correlation_id=ctx.correlation_id,
        task_type=ctx.task_type,
        model_name="evidenced-model",
        quality_gate_passed=True,
        attempts=[
            {
                "quality_gate_passed": True,
                "model_id": "evidenced-model",
                "backend_id": "local",
                "tier": "local",
            }
        ],
    ).model_dump(mode="json")
    projection.handle(
        {
            **success,
            "_event_type": "onex.evt.omnimarket.delegate-skill-completed.v1",
            "_db": db,
        }
    )
    projection.handle({**event, "_db": db})
    row = db.query(TABLE)[0]
    assert row["terminal_ok"] is True
    assert row["terminal_failure_cause"] is None


@pytest.mark.parametrize("requested", [None, 1, 240, 241])
def test_handler_context_uses_resolved_budget_margin_and_contract_grace(requested):
    from omnimarket.inference.task_class_authority import (
        resolve_task_class_execution_budget,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
        _request_reap_context,
    )

    request = ModelDelegateSkillRequest(
        prompt="test",
        task_type="test",
        source="claude-code",
        requested_timeout_seconds=requested,
        tenant_id="omninode",
    )
    before = datetime.now(UTC)
    context = _request_reap_context(request)
    after = datetime.now(UTC)
    assert context is not None
    budget = resolve_task_class_execution_budget(request.task_type)
    execution = min(
        requested or budget.task_class_timeout_ceiling_seconds,
        budget.task_class_timeout_ceiling_seconds,
    )
    delta = timedelta(seconds=execution + budget.terminal_delivery_margin_seconds + 60)
    assert before + delta <= context.deadline_at <= after + delta
    assert context.tenant_id == request.tenant_id


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"grace_seconds": 0, "max_reaps_per_tick": 25, "scan_interval_seconds": 15},
        {"grace_seconds": 60, "max_reaps_per_tick": "25", "scan_interval_seconds": 15},
        {"grace_seconds": 60, "max_reaps_per_tick": 25},
    ],
)
def test_reaper_config_refuses_missing_and_invalid_declarations(tmp_path, config):
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
        load_delegation_reaper_config,
    )

    path = tmp_path / "contract.yaml"
    path.write_text(yaml.safe_dump({"delegation_reaper": config}))
    with pytest.raises(ValueError, match=r"delegation_reaper|validation error"):
        load_delegation_reaper_config(path)


async def test_scan_is_throttled_to_the_contract_interval(monkeypatch, caplog):
    _, port, _, _, ctx = _setup()
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegation_reaper import (
        HandlerDelegationReaper,
    )
    from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
        ModelDelegationReaperConfig,
    )

    scans = []
    stalled = port.stalled_claims

    def counting(*, now, limit):
        scans.append(now)
        return stalled(now=now, limit=limit)

    monkeypatch.setattr(port, "stalled_claims", counting)
    reaper = HandlerDelegationReaper(
        port=port,
        config=ModelDelegationReaperConfig(
            grace_seconds=60, max_reaps_per_tick=25, scan_interval_seconds=15
        ),
    )
    start = ctx.deadline_at - timedelta(minutes=5)
    with caplog.at_level("INFO"):
        for seconds in (0, 1, 14, 15, 16, 30):
            await reaper.handle(_tick(start + timedelta(seconds=seconds)))
    assert [(s - start).total_seconds() for s in scans] == [0, 15, 30]
    started = [r for r in caplog.records if "Delegation reaper scanning" in r.message]
    assert len(started) == 1


def test_context_refuses_naive_deadline_and_normalizes_aware_deadline():
    _, _, _, _, context = _setup()
    data = context.model_dump()
    data["deadline_at"] = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        type(context).model_validate(data)
    data["deadline_at"] = "2026-01-01T05:00:00+05:00"
    assert type(context).model_validate(data).deadline_at == datetime(
        2026, 1, 1, tzinfo=UTC
    )


async def test_one_failed_row_does_not_stop_others_and_is_retried_next_tick(
    monkeypatch,
):
    db, port, reaper, first, ctx = _setup()
    second = uuid4()
    port.claim(delivery_id=second, correlation_id=uuid4(), reap_context=ctx)
    reap = port.reap

    def fail_first(*, delivery_id, terminal):
        if delivery_id == first:
            raise RuntimeError("temporary store failure")
        return reap(delivery_id=delivery_id, terminal=terminal)

    monkeypatch.setattr(port, "reap", fail_first)
    output = await reaper.handle(_tick(ctx.deadline_at))
    assert output is not None
    assert len(output.events) == 1
    assert output.metrics["failed_count"] == 1
    assert _row(db, str(first))["terminal_json"] == ""
    monkeypatch.setattr(port, "reap", reap)
    output = await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=1)))
    assert output is not None
    assert len(output.events) == 1
    assert await reaper.handle(_tick(ctx.deadline_at + timedelta(seconds=2))) is None


def test_stalled_selection_is_deadline_ordered_bounded_and_skips_bad_context():
    db, port, _, first, ctx = _setup()
    deliveries = [uuid4() for _ in range(3)]
    for index, delivery_id in enumerate(deliveries):
        context = ctx.model_copy(
            update={"deadline_at": ctx.deadline_at - timedelta(seconds=index + 1)}
        )
        port.claim(
            delivery_id=delivery_id, correlation_id=uuid4(), reap_context=context
        )
    db.upsert(
        CLAIMS_TABLE,
        "delivery_id",
        {"delivery_id": f"reap:{first}", "terminal_json": "invalid"},
    )
    stalled = port.stalled_claims(now=ctx.deadline_at, limit=2)
    assert [claim.delivery_id for claim in stalled] == list(reversed(deliveries))[:2]
    assert port.stalled_claims(now=ctx.deadline_at, limit=0) == []


@pytest.mark.parametrize("raw", ["invalid", "[]", "null"])
def test_undecodable_slot_is_held_and_late_evidence_is_first_writer_wins(raw):
    db, port, _, delivery_id, ctx = _setup()
    db.upsert(
        CLAIMS_TABLE,
        "delivery_id",
        {
            "delivery_id": f"slot:{delivery_id}",
            "claimed_at": datetime.now(UTC).isoformat(),
            "correlation_id": str(ctx.correlation_id),
            "terminal_json": raw,
        },
    )
    terminal = ModelDelegateSkillCompleted(
        correlation_id=ctx.correlation_id, task_type=ctx.task_type
    )
    result = port.record_terminal(delivery_id=delivery_id, terminal=_record(terminal))
    assert not result.won
    assert result.held == {}
    changed = terminal.model_copy(update={"response": "later attempt"})
    assert not port.record_terminal(
        delivery_id=delivery_id, terminal=_record(changed)
    ).won
    # Two late results of one status are two pieces of evidence, not one.
    assert sorted(
        json.loads(row["terminal_json"])["data"]["response"]
        for row in _late_rows(db, delivery_id)
    ) == ["", "later attempt"]
    assert _row(db, f"slot:{delivery_id}")["terminal_json"] == raw


def test_concurrent_worker_and_reaper_have_one_sqlite_slot_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

    db, port, _, delivery_id, ctx = _setup(
        database=SqliteDatabaseAdapter(tmp_path / "claims.sqlite")
    )
    real = _record(
        ModelDelegateSkillCompleted(
            correlation_id=ctx.correlation_id, task_type=ctx.task_type
        )
    )
    reaped = _record(
        ModelDelegateSkillFailed(
            correlation_id=ctx.correlation_id,
            task_type=ctx.task_type,
            terminal_failure_cause="no_terminal",
        )
    )
    barrier = Barrier(2)

    def worker():
        barrier.wait(timeout=5)
        return port.record_terminal(delivery_id=delivery_id, terminal=real)

    def reaper():
        barrier.wait(timeout=5)
        return port.reap(delivery_id=delivery_id, terminal=reaped)

    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_future = pool.submit(worker)
        reaper_future = pool.submit(reaper)
        worker_outcome = worker_future.result(timeout=10)
        reaper_outcome = reaper_future.result(timeout=10)
    assert int(worker_outcome.won) + int(reaper_outcome.won) == 1
    held = json.loads(_row(db, f"slot:{delivery_id}")["terminal_json"])
    assert held == (real if worker_outcome.won else reaped)
    assert json.loads(_row(db, str(delivery_id))["terminal_json"]) == held
    if not worker_outcome.won:
        (late,) = _late_rows(db, delivery_id)
        assert json.loads(late["terminal_json"]) == real
