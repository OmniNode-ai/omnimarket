# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: item snapshots reach S5/S6, one recovery, dispatch and G2.

The chain runs with injected dispatch and both memory and SQLite claim stores.
It does not assert a runtime bus roundtrip or deployed Stop/age entrypoints.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.item_response import g2_unanswered
from omnimarket.models.item_state_check import (
    ModelItem,
    ModelItemAuthority,
    ModelItemLane,
    ModelItemPark,
    ModelItemPromotion,
    ModelItemRecoveryRequest,
    ModelItemSnapshot,
    ModelItemTickRequest,
    ModelRecoveryRecommendation,
)
from omnimarket.nodes.node_item_lane_miss_compute import NodeItemLaneMissCompute
from omnimarket.nodes.node_item_recovery_plan_compute import NodeItemRecoveryPlanCompute
from omnimarket.nodes.node_item_state_check_effect import NodeItemStateCheckEffect
from omnimarket.nodes.node_item_state_check_effect.handlers.item_dispatch import (
    MemoryItemDispatch,
)
from omnimarket.nodes.node_item_state_check_effect.handlers.item_recovery_store import (
    MemoryItemRecoveryStore,
    SqliteItemRecoveryStore,
)
from omnimarket.nodes.node_item_unanswered_compute import NodeItemUnansweredCompute

NOW = datetime(2026, 11, 4, 21, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("state", "owner"),
    [("dropped", ""), ("ticketed", "orchestrator"), ("new", "orchestrator")],
)
def test_item_state_check_no_lane_is_actionable_even_inside_grace(state, owner):
    item = ModelItem(
        item_id="item-1",
        words="Do the work",
        kind="ask",
        state=state,
        owner=owner,
        session_id="session",
        message_id="message",
        repository="market",
        memory_limit_mb=8192,
    )
    result = NodeItemUnansweredCompute().handle(
        ModelItemSnapshot(items=(item,), now=NOW)
    )
    assert len(result.findings) == 1
    assert result.findings[0].class_key == "item-unanswered"


def item(**changes):
    values = {
        "item_id": "item-1",
        "words": "Do the work",
        "kind": "ask",
        "state": "dropped",
        "owner": "orchestrator",
        "session_id": "session",
        "message_id": "message",
        "repository": "market",
        "memory_limit_mb": 8192,
    }
    return ModelItem(**(values | changes))


def policy():
    p = NodeItemUnansweredCompute().policy
    return p.model_copy(
        update={
            "rules": {
                k: r.model_copy(update={"mode": "dispatch"}) for k, r in p.rules.items()
            }
        }
    )


def tick(items, *, tick_id="tick-1", slots=3, recommendations=(), promotions=True):
    return ModelItemTickRequest(
        snapshot=ModelItemSnapshot(items=tuple(items), now=NOW, tick_id=tick_id),
        free_slots=slots,
        recommendations=recommendations,
        promotions=tuple(
            ModelItemPromotion(
                rule_id=k, mode="dispatch", evidence="measured-gate-receipt"
            )
            for k in ("S5", "S6")
        )
        if promotions
        else (),
    )


@pytest.fixture(params=["inmemory", "durable"])
def runner(request, tmp_path):
    store = (
        MemoryItemRecoveryStore()
        if request.param == "inmemory"
        else SqliteItemRecoveryStore(str(tmp_path / "scratch-ledger.sqlite"))
    )
    io = MemoryItemDispatch("operator-question")
    return NodeItemStateCheckEffect(store, io, policy()), store, io


@pytest.mark.parametrize(
    ("state", "owner"),
    [
        ("dropped", ""),
        ("new", "orchestrator"),
        ("ticketed", "orchestrator"),
        ("parked", "orchestrator"),
        ("owned", "orchestrator"),
    ],
)
def test_item_state_check_five_controlled_ticks_dispatch_once_and_print_owner(
    runner, state, owner
):
    handler, store, io = runner
    request = tick([item(state=state, owner=owner)])
    result = handler.handle(request)
    assert len(result.started) == 1
    lane = result.started[0].lane
    assert lane
    assert any(f"started {lane}" in line for line in result.lines)
    assert not result.g2_unanswered
    # A fresh stale fold must also read the durable ownership receipt.
    for n in range(2, 6):
        result = handler.handle(
            request.model_copy(
                update={
                    "snapshot": request.snapshot.model_copy(
                        update={
                            "tick_id": f"tick-{n}",
                            "now": NOW + timedelta(minutes=n),
                        }
                    )
                }
            )
        )
        assert not result.started
        assert any(f"owned by {lane}" in line for line in result.lines)
    assert len([r for r in store.rows if r.startswith("CLAIM ")]) == 1
    assert len(io.started) == 1


@pytest.mark.parametrize(
    "kind", ["ask", "idea", "promise", "ruling", "question", "message", "unclassified"]
)
@pytest.mark.parametrize("state", ["new", "dropped", "owned", "parked", "ticketed"])
def test_item_state_check_all_open_kinds_and_states(kind, state):
    snapshot = tick([item(kind=kind, state=state)]).snapshot
    result = NodeItemUnansweredCompute().handle(snapshot)
    assert len(result.findings) == 1
    assert g2_unanswered(snapshot, policy()) == ("item-1",)


@pytest.mark.parametrize("authority", ["HOLD", "RULING"])
@pytest.mark.parametrize(("minutes", "expected"), [(-1, True), (0, True), (1, False)])
def test_item_state_check_park_requires_cited_authority_and_unexpired_review(
    authority, minutes, expected
):
    park = ModelItemPark(
        notice="Parked until review",
        authority_kind=authority,
        authority_stamp=NOW - timedelta(days=1),
        review_by=NOW + timedelta(minutes=minutes),
    )
    snapshot = tick([item(state="parked", park=park)]).snapshot.model_copy(
        update={
            "authorities": (
                ModelItemAuthority(kind=authority, stamp=park.authority_stamp),
            )
        }
    )
    assert bool(NodeItemUnansweredCompute().handle(snapshot).findings) is expected
    assert bool(g2_unanswered(snapshot, policy())) is expected
    # A citation absent from the authoritative ledger snapshot is no park.
    missing = snapshot.model_copy(update={"authorities": ()})
    assert len(NodeItemUnansweredCompute().handle(missing).findings) == 1
    assert g2_unanswered(missing, policy()) == ("item-1",)


def test_item_state_check_five_items_three_slots_two_positioned_notices(runner):
    handler, store, _io = runner
    values = [item(item_id=f"item-{n}") for n in range(5)]
    result = handler.handle(tick(values))
    assert len(result.started) == 3
    assert [(key, notice.position, notice.reason) for key, notice in result.queued] == [
        ("item-3", 1, "capacity"),
        ("item-4", 2, "capacity"),
    ]
    assert not result.g2_unanswered
    assert len([r for r in store.rows if r.startswith("CLAIM ")]) == 3
    assert sum("state=queued" in line for line in result.lines) == 2
    # Queue notices answer this tick; on the next tick capacity is tried again.
    result2 = handler.handle(tick(result.snapshot.items, tick_id="tick-2", slots=2))
    assert len(result2.started) == 2
    assert not result2.g2_unanswered


def test_item_state_check_operator_question_once_across_three_ticks(runner):
    handler, store, io = runner
    value = item(kind="question", owner="operator", running_lane="irrelevant")
    for n in range(3):
        result = handler.handle(tick([value], tick_id=f"tick-{n}", slots=0))
        assert not result.g2_unanswered
    assert len(io.started) == 1
    receipt = next(iter(io.receipts.values()))
    assert receipt.lane is None
    assert len([r for r in store.rows if "state=asked" in r]) == 1


def test_item_state_check_retry_merges_repository_and_memory_then_escalates_once(
    runner,
):
    handler, store, io = runner
    value = item(
        state="owned",
        ended_lane=ModelItemLane(lane="failed", outcome="blocked", attempt=0),
    )
    recommendations = tuple(
        ModelRecoveryRecommendation(
            item_id=value.item_id, digest=value.digest, source=source, **changes
        )
        for source, changes in [
            ("reroute", {"repository": "correct-repo"}),
            ("oom", {"memory_limit_mb": 16384}),
        ]
    )
    result = handler.handle(tick([value], recommendations=recommendations))
    assert {f.rule_id for f in result.findings} == {"S5", "S6"}
    assert len(result.started) == 1
    plan = next(iter(io.started.values()))
    assert (plan.repository, plan.memory_limit_mb, plan.brief, plan.attempt) == (
        "correct-repo",
        16384,
        value.words,
        1,
    )
    assert plan.cause == f"item@{value.item_id}@{value.digest}#1"
    failed = value.model_copy(
        update={
            "ended_lane": ModelItemLane(
                lane=result.started[0].lane, outcome="blocked", attempt=1
            )
        }
    )
    for n in range(3):
        escalated = handler.handle(
            tick([failed], tick_id=f"after-{n}", recommendations=recommendations)
        )
        assert not escalated.g2_unanswered
    assert len(io.started) == 2
    escalation = list(io.started.values())[-1]
    assert escalation.escalation
    assert escalation.action == "operator-question"
    assert len([r for r in store.rows if r.startswith("BUDGET ")]) == 2
    assert any(d.reason == "budget" for _, d in escalated.queued)


def test_item_state_check_digest_changes_family_and_stale_advice_is_ignored():
    value = item()
    revised = item(words="Different work")
    assert revised.family != value.family
    findings = NodeItemUnansweredCompute().handle(tick([revised]).snapshot).findings
    stale = ModelRecoveryRecommendation(
        item_id=value.item_id, digest=value.digest, source="oom", memory_limit_mb=32768
    )
    plan = NodeItemRecoveryPlanCompute().handle(
        ModelItemRecoveryRequest(
            item=revised, findings=findings, recommendations=(stale,)
        )
    )
    assert plan.memory_limit_mb == revised.memory_limit_mb


@pytest.mark.parametrize(
    ("done", "outcome"), [(True, "blocked"), (False, "done"), (False, "merged")]
)
def test_item_state_check_lane_done_is_no_s6(done, outcome):
    value = item(
        ended_lane=ModelItemLane(lane="ended", outcome=outcome, attempt=0, done=done)
    )
    assert not NodeItemLaneMissCompute().handle(tick([value]).snapshot).findings


def test_item_state_check_shadow_prints_would_have_sent_and_never_claims(runner):
    _, store, io = runner
    for p, request in [
        (NodeItemUnansweredCompute().policy, tick([item()], promotions=False)),
        (policy(), tick([item()], promotions=False)),
    ]:
        result = NodeItemStateCheckEffect(store, io, p).handle(request)
        assert "would-have-sent fill-backlog-item" in result.lines[0]
        assert not result.started
        assert not store.rows
        assert not io.started


def test_item_state_check_three_answer_entrypoints_race_real_claim_store(tmp_path):
    path = str(tmp_path / "shared.sqlite")
    io = MemoryItemDispatch("operator-question")
    value = item(kind="message")

    def entrypoint(name):
        return NodeItemStateCheckEffect(
            SqliteItemRecoveryStore(path), io, policy()
        ).handle(tick([value], tick_id=name))

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(entrypoint, ["stop", "age", "S5"]))
    assert sum(len(r.started) for r in results) == 1
    assert tuple(plan.cause for plan in io.started.values()) == (
        "answer@session@message",
    )
    rows = SqliteItemRecoveryStore(path).rows
    assert len([r for r in rows if r.startswith("CLAIM ")]) == 1
    assert len([r for r in rows if r.startswith("BUDGET ")]) == 1


def test_item_state_check_ended_answer_lane_is_replaced_with_shared_cause(runner):
    handler, store, io = runner
    value = item(kind="message")
    initial = handler.handle(tick([value]))
    ended = value.model_copy(
        update={
            "ended_lane": ModelItemLane(
                lane=initial.started[0].lane, outcome="blocked", attempt=0
            )
        }
    )
    retry = handler.handle(tick([ended], tick_id="retry"))
    assert len(retry.started) == 1
    assert retry.started[0].lane != initial.started[0].lane
    assert all(plan.cause == "answer@session@message" for plan in io.started.values())
    assert len([r for r in store.rows if r.startswith("CLAIM ")]) == 2


def test_item_state_check_durable_ack_cannot_resurrect_ended_lane(runner):
    handler, _store, _io = runner
    value = item()
    initial = handler.handle(tick([value]))
    ended = value.model_copy(
        update={
            "ended_lane": ModelItemLane(
                lane=initial.started[0].lane, outcome="blocked", attempt=0
            )
        }
    )
    result = handler.handle(tick([ended], tick_id="retry"))
    assert len(result.started) == 1
    assert result.started[0].lane != initial.started[0].lane


@pytest.mark.parametrize(
    ("node_name", "completed_topic"),
    [
        (
            "node_item_unanswered_compute",
            "onex.evt.omnimarket.item-unanswered-compute-completed.v1",
        ),
        (
            "node_item_lane_miss_compute",
            "onex.evt.omnimarket.item-lane-miss-compute-completed.v1",
        ),
        (
            "node_item_recovery_plan_compute",
            "onex.evt.omnimarket.item-recovery-plan-compute-completed.v1",
        ),
        (
            "node_item_state_check_effect",
            "onex.evt.omnimarket.item-state-check-effect-completed.v1",
        ),
    ],
)
async def test_item_state_check_runtime_command(
    node_name, completed_topic, monkeypatch
):
    """Recorded contracts must advertise only terminals their handlers return."""
    import asyncio
    import importlib.resources
    import json
    from uuid import uuid4

    import yaml
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        installed_overlay,
        load_overlay_bindings,
    )

    package = importlib.resources.files(f"omnimarket.nodes.{node_name}")
    contract_path = package.joinpath("contract.yaml")
    contract = yaml.safe_load(contract_path.read_text())
    assert contract["event_bus"]["publish_topics"] == [completed_topic]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": completed_topic
    }
    previous_overlay = installed_overlay()
    effect_package = importlib.resources.files(
        "omnimarket.nodes.node_item_state_check_effect"
    )
    from pathlib import Path

    bind_overlay(
        load_overlay_bindings(
            Path(str(effect_package.joinpath("inmemory.overlay.yaml")))
        )
    )
    monkeypatch.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
    bus = EventBusInmemory(environment="local", group="item-state-check-wiring")
    await bus.start()
    completed = asyncio.Queue()

    async def collect(message):
        await completed.put(json.loads(message.value))

    try:
        await bus.subscribe(
            completed_topic, group_id="item-results", on_message=collect
        )
        discovered = discover_contracts_from_paths([Path(str(contract_path))])
        assert not discovered.errors
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert report.total_wired == 1
        assert not report.quarantined_handlers
        engine.freeze()
        sample = item(
            ended_lane=ModelItemLane(lane="ended-lane", outcome="blocked", attempt=0)
        )
        snapshot = ModelItemSnapshot(items=(sample,), now=NOW)
        if node_name == "node_item_recovery_plan_compute":
            request = ModelItemRecoveryRequest(
                item=sample,
                findings=NodeItemUnansweredCompute().handle(snapshot).findings,
            )
        elif node_name == "node_item_state_check_effect":
            request = tick([sample], promotions=False)
        else:
            request = snapshot
        correlation_id = uuid4()
        command_topic = contract["runtime_dispatch"]["command_topic"]
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation_id,
            event_type=command_topic.removeprefix("onex.cmd.").removesuffix(".v1"),
        )
        await bus.publish(
            command_topic, None, envelope.model_dump_json().encode(), None
        )
        event = await asyncio.wait_for(completed.get(), timeout=5)
        assert event["correlation_id"] == str(correlation_id)
        payload = event["payload"]
        if node_name == "node_item_recovery_plan_compute":
            assert payload["item_id"] == "item-1"
            assert payload["action"] == "fill-backlog-item"
        elif node_name == "node_item_state_check_effect":
            assert payload["started"] == []
            assert "would-have-sent" in payload["lines"][0]
        else:
            assert payload["findings"][0]["item_id"] == "item-1"
    finally:
        await bus.close()
        bind_overlay(previous_overlay)
