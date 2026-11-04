# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-subject cause admission and item-bound process-fill falsifiers."""

from datetime import UTC, datetime
from importlib.resources import files

import pytest
import yaml

from omnimarket.models.standing_rules.model_standing_rules import (
    ModelStandingRulesDispatch,
    ModelStandingRulesRequest,
)
from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.handler_dispatch_standing_rules_effect import (
    HandlerDispatchStandingRulesEffect,
)
from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.standing_rules_io import (
    MemoryStandingRulesIO,
)

pytestmark = pytest.mark.unit


class Documents:
    def read(self, name):
        return yaml.safe_load(
            files("omnimarket.nodes.node_dispatch_standing_rules_effect")
            .joinpath("standing_rules.yaml")
            .read_text()
        )


def dispatch(subject, request_id):
    return ModelStandingRulesDispatch(
        request_id=request_id,
        lane=request_id,
        dispatch_kind="agent",
        model="codex",
        repo_only=True,
        placement="lab",
        subject=subject,
        cause={"repository": "example", "class_key": "shared-red", "base": "dev"},
    )


def test_rule_cause_idle_two_subjects_cannot_both_start():
    io = MemoryStandingRulesIO(
        {"ledger.md": "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go"}
    )
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.cause_claim_store import (
        MemoryCauseClaimStore,
    )

    handler = HandlerDispatchStandingRulesEffect(
        Documents(), io, MemoryCauseClaimStore()
    )
    results = [
        handler.handle(
            ModelStandingRulesRequest(
                dispatch=dispatch(subject, lane),
                now=datetime(2026, 11, 4, 17, tzinfo=UTC),
            )
        )
        for subject, lane in (
            ("item:one", "item-lane"),
            ("pull-request:two", "pr-lane"),
        )
    ]
    assert sum(r.verdict == "allow" for r in results) == 1
    assert results[1].verdict == "refuse"
    assert "item-lane" in results[1].fix


def process_input():
    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelProcessFixInput,
    )

    rule = next(
        r for r in Documents().read("standing_rules")["rules"] if r["id"] == "R-D"
    )
    return ModelProcessFixInput.model_validate(
        {
            "rule": rule,
            "items": [
                {
                    "item_id": f"item-{n}",
                    "kind": kind,
                    "ruling_kind": "process",
                    "state": "parked",
                    "work": {
                        "kind": "process-fix",
                        "key": f"item-{n}",
                        "repo": "example",
                        "ticket": "OMN-19998",
                        "title": f"process fix {n}",
                    },
                }
                for n, kind in enumerate(("ask", "idea", "promise", "ruling", "ask"), 1)
            ],
        }
    )


def test_rule_cause_idle_process_items_precede_all_backlog():
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        candidates,
    )

    ordered = candidates(
        ["MISSING host-tick:test | broken"],
        [{"key": "other", "kind": "other"}],
        process_fixes=process_input(),
    )
    assert [item["key"] for item in ordered[:5]] == [f"item-{n}" for n in range(1, 6)]


@pytest.mark.parametrize(
    ("count", "unrelated", "refuse"),
    [(1, False, True), (3, False, False), (3, True, True)],
)
def test_rule_cause_idle_stop_requires_bound_starts(count, unrelated, refuse):
    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelAcceptedItemStart,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleTurnGuard,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
        ModelIdleTurnGuardRequest,
    )

    request = ModelIdleTurnGuardRequest(
        tool_uses=[
            {
                "id": "tick",
                "name": "Bash",
                "input": {"command": "python3 throughput_tick.py"},
            },
            {"id": "dispatch", "name": "Agent", "input": {}},
        ],
        tool_results=[
            {
                "id": "tick",
                "text": "MISSING lab-headroom:lab-a | free Claude slots 3 | FIX: dispatch",
            }
        ],
        process_fixes=process_input(),
        accepted_starts=tuple(
            ModelAcceptedItemStart(
                lane=f"lane-{n}", for_item=f"{'other' if unrelated else 'item'}-{n}"
            )
            for n in range(1, count + 1)
        ),
    )
    result = HandlerIdleTurnGuard().handle(request)
    assert result.refuse is refuse


def _race_dispatch(path, subject, lane, barrier, answers):
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.cause_claim_store import (
        SqliteCauseClaimStore,
    )

    store = SqliteCauseClaimStore(path, 10)
    io = MemoryStandingRulesIO(
        {"ledger.md": "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go"}
    )
    handler = HandlerDispatchStandingRulesEffect(Documents(), io, store)
    barrier.wait(timeout=15)
    result = handler.handle(
        ModelStandingRulesRequest(
            dispatch=dispatch(subject, lane), now=datetime(2026, 11, 4, 17, tzinfo=UTC)
        )
    )
    answers.put(result.model_dump())


def test_rule_cause_idle_two_processes_real_claim_store(tmp_path):
    import multiprocessing

    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.cause_claim_store import (
        SqliteCauseClaimStore,
    )

    path = str(tmp_path / "part-a-claims.sqlite3")
    SqliteCauseClaimStore(path, 10)
    context = multiprocessing.get_context("spawn")
    barrier, answers = context.Barrier(2), context.Queue()
    processes = [
        context.Process(
            target=_race_dispatch, args=(path, subject, lane, barrier, answers)
        )
        for subject, lane in (
            ("item:one", "item-lane"),
            ("pull-request:two", "pr-lane"),
        )
    ]
    try:
        for process in processes:
            process.start()
        results = [answers.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
        assert sorted(row["verdict"] for row in results) == ["allow", "refuse"]
        loser = next(row for row in results if row["verdict"] == "refuse")
        winner = next(row for row in results if row["verdict"] == "allow")
        assert loser["owner"] == winner["request_id"]
        assert loser["cause_key"] == "cause=example@shared-red@dev"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        answers.close()
        answers.join_thread()


@pytest.fixture
def fill_overlay(monkeypatch):
    from tests.nodes.node_idle_slot_fill_effect.overlay_fixture import ENV

    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", ENV["ONEX_SKILL_OVERLAY_ROOTS"])


def test_rule_cause_idle_fill_three_and_queue_four_five(fill_overlay):
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleSlotFill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
        ModelIdleSlotFillRequest,
    )

    result = HandlerIdleSlotFill().handle(
        ModelIdleSlotFillRequest(
            now="2026-11-04T17:00:00Z",
            hosts=[{"name": "lab-a", "admission": {"claude_slots": 3}}],
            lines=["MISSING lab-headroom:lab-a | free Claude slots 3 | FIX: dispatch"],
            process_fixes=process_input(),
            backlog=[
                {
                    "kind": "ticket",
                    "key": "ordinary",
                    "ticket": "OMN-19998",
                    "repo": "example",
                }
            ],
        )
    )
    assert [send["for_item"] for send in result.sends] == ["item-1", "item-2", "item-3"]
    assert [(notice.item_id, notice.position) for notice in result.queue_notices] == [
        ("item-4", 4),
        ("item-5", 5),
    ]
    assert result.idle_after == 0
    assert all(f"for-item={send['for_item']}" in send["brief"] for send in result.sends)


def test_rule_cause_idle_fill_effect_records_queue_notices(
    tmp_path, monkeypatch, fill_overlay
):
    import json
    from types import SimpleNamespace

    from omnimarket.nodes.node_idle_slot_fill_effect.handlers import (
        handler_idle_slot_fill as fill,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
        ModelLabFillRequest,
    )
    from tests.nodes.node_idle_slot_fill_effect.test_idle_slot_fill_node import (
        stub_runner,
    )

    monkeypatch.setattr(fill, "read_fixer_holds", lambda *_args: ({}, ""))
    monkeypatch.setattr(fill, "fill_wip_gate", lambda **_kwargs: (None, None))
    monkeypatch.setattr(fill, "default_sources", lambda *_args: None)
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "kind": "ticket",
                        "key": "ordinary",
                        "ticket": "OMN-19998",
                        "repo": "example",
                    }
                ]
            }
        )
    )
    records = []
    recovery = SimpleNamespace(
        handle=lambda **_kwargs: SimpleNamespace(found=[], items=[])
    )
    result = fill.HandlerLabFill(recovery).handle(
        ModelLabFillRequest(
            now="2026-11-04T17:00:00Z",
            request={
                "lab_headroom": {
                    "hosts": [{"name": "lab-a", "admission": {"claude_slots": 3}}]
                }
            },
            lines=["MISSING lab-headroom:lab-a | free Claude slots 3 | FIX: dispatch"],
            launch=None,
            state_dir=tmp_path / "state",
            backlog=str(backlog),
            runner=stub_runner(tmp_path),
            process_fixes=process_input(),
            record=lambda *row: records.append(row),
        )
    )
    calls = [
        json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()
    ]
    assert len(calls) == 3
    assert [row for row in records if row[0] == "process-fix-queue"] == [
        ("process-fix-queue", "item-4", "position=4; no free slot"),
        ("process-fix-queue", "item-5", "position=5; no free slot"),
    ]
    assert any("sent=3" in row[2] for row in result.found)


def test_rule_cause_idle_no_process_item_keeps_old_behavior():
    from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
        HandlerIdleTurnGuard,
    )
    from omnimarket.nodes.node_idle_slot_fill_effect.models.model_idle_slot_fill import (
        ModelIdleTurnGuardRequest,
    )

    source = process_input().model_copy(update={"items": ()})
    result = HandlerIdleTurnGuard().handle(
        ModelIdleTurnGuardRequest(
            [
                {
                    "id": "tick",
                    "name": "Bash",
                    "input": {"command": "python3 throughput_tick.py"},
                },
                {"name": "Agent", "input": {}},
            ],
            [
                {
                    "id": "tick",
                    "text": "MISSING lab-headroom:lab-a | free Claude slots 3 | FIX: dispatch",
                }
            ],
            source,
        )
    )
    assert result.refuse is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "preference"),
        ("ruling_kind", "decision"),
        ("state", "done"),
        ("state", "withdrawn"),
        ("running_lane", "live-lane"),
    ],
)
def test_rule_cause_idle_item_eligibility(field, value):
    from omnimarket.nodes.node_dispatch_standing_rules_compute import (
        eligible_process_fixes,
    )

    source = process_input()
    item = source.items[0].model_copy(update={field: value})
    assert eligible_process_fixes(source.model_copy(update={"items": (item,)})) == ()


def test_rule_cause_idle_fold_reads_claims_in_turn_only():
    import json

    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelProcessFixReadRequest,
    )
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.handler_process_fix_read import (
        HandlerProcessFixRead,
    )

    io = MemoryStandingRulesIO(
        {
            "process-items.json": json.dumps(
                [item.model_dump(mode="json") for item in process_input().items]
            ),
            "ledger.md": "\n".join(
                [
                    "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go",
                    "2026-11-04T15:59:59Z | CLAIM | lane=old | for-item=item-1",
                    "2026-11-04T16:01:00Z | CLAIM | lane=one | for-item=item-1",
                    "2026-11-04T16:01:01Z | CLAIM | lane=two | ask=item-2",
                    "2026-11-04T16:01:02Z | STATUS | lane=fake | for-item=item-3",
                    "2026-11-04T17:00:01Z | CLAIM | lane=future | ask=item-4",
                ]
            ),
        }
    )
    result = HandlerProcessFixRead(Documents(), io).handle(
        ModelProcessFixReadRequest(
            request_id="turn",
            now="2026-11-04T17:00:00Z",
            turn_started_at="2026-11-04T16:00:00Z",
        )
    )
    assert [row.lane for row in result.accepted_starts] == ["one", "two"]
    assert result.process_fixes == process_input()


@pytest.mark.parametrize("profile", ["inmemory", "redpanda"])
def test_rule_cause_idle_overlay_admission_and_fold(tmp_path, profile):
    import json

    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        load_overlay_bindings,
    )
    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelProcessFixReadRequest,
    )
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.handler_process_fix_read import (
        HandlerProcessFixRead,
    )

    root = files("omnimarket.nodes.node_dispatch_standing_rules_effect")
    bindings = yaml.safe_load(root.joinpath(f"overlay.{profile}.yaml").read_text())
    registry = Documents().read("standing_rules")
    registry["audit_path"] = str(tmp_path / "audit.jsonl")
    docs = {
        "ledger.md": "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go",
        "process-items.json": json.dumps(
            [item.model_dump(mode="json") for item in process_input().items]
        ),
    }
    if profile == "inmemory":
        bindings["dependencies"][0]["config"] = {
            "standing_rules": yaml.safe_dump(registry)
        }
        bindings["dependencies"][1]["config"] = docs
        bindings["dependencies"][2]["config"] = {"namespace": str(tmp_path)}
    else:
        for path, value in docs.items():
            (tmp_path / path).write_text(value)
        registry["ledger"]["path"] = str(tmp_path / "ledger.md")
        next(rule for rule in registry["rules"] if rule["check"] == "process_fill")[
            "item_fold_path"
        ] = str(tmp_path / "process-items.json")
        (tmp_path / "standing_rules.yaml").write_text(yaml.safe_dump(registry))
        bindings["dependencies"][0]["config"] = {
            "standing_rules": str(tmp_path / "standing_rules.yaml")
        }
        bindings["dependencies"][2]["config"]["path"] = str(tmp_path / "claims.sqlite3")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(bindings))
    bind_overlay(load_overlay_bindings(overlay))
    try:
        # Separate handler instances must still share the overlay's claim store.
        first = HandlerDispatchStandingRulesEffect().handle(
            ModelStandingRulesRequest(
                dispatch=dispatch("item:one", "one"), now="2026-11-04T17:00:00Z"
            )
        )
        second = HandlerDispatchStandingRulesEffect().handle(
            ModelStandingRulesRequest(
                dispatch=dispatch("pr:two", "two"), now="2026-11-04T17:00:00Z"
            )
        )
        assert first.verdict == "allow"
        assert second.verdict == "refuse"
        assert second.owner == "one"
        read = HandlerProcessFixRead().handle(
            ModelProcessFixReadRequest(request_id="read", now="2026-11-04T17:00:00Z")
        )
        assert [item.item_id for item in read.process_fixes.items] == [
            f"item-{n}" for n in range(1, 6)
        ]
    finally:
        bind_overlay(None)


def test_rule_cause_idle_replayed_request_cannot_start_twice():
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.cause_claim_store import (
        MemoryCauseClaimStore,
    )

    io = MemoryStandingRulesIO(
        {"ledger.md": "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go"}
    )
    handler = HandlerDispatchStandingRulesEffect(
        Documents(), io, MemoryCauseClaimStore()
    )
    request = ModelStandingRulesRequest(
        dispatch=dispatch("item:one", "one"), now="2026-11-04T17:00:00Z"
    )
    assert handler.handle(request).verdict == "allow"
    assert handler.handle(request).verdict == "refuse"


def test_rule_cause_idle_missing_fold_is_unread():
    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelProcessFixReadRequest,
    )
    from omnimarket.nodes.node_dispatch_standing_rules_effect.handlers.handler_process_fix_read import (
        HandlerProcessFixRead,
    )

    io = MemoryStandingRulesIO(
        {"ledger.md": "2026-11-04T12:57:20Z | RULING | lane=orchestrator-31d9 | go"}
    )
    with pytest.raises(FileNotFoundError):
        HandlerProcessFixRead(Documents(), io).handle(
            ModelProcessFixReadRequest(request_id="read", now="2026-11-04T17:00:00Z")
        )


async def test_rule_cause_idle_process_read_runtime_topic(tmp_path):
    import asyncio
    import json
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    from omnimarket.handlers.container.container_overlay_dependencies import (
        bind_overlay,
        load_overlay_bindings,
    )
    from omnimarket.models.standing_rules.model_rule_cause_idle import (
        ModelProcessFixReadRequest,
    )

    root = files("omnimarket.nodes.node_dispatch_standing_rules_effect")
    contract = yaml.safe_load(root.joinpath("contract.yaml").read_text())
    operation = next(
        entry
        for entry in contract["handler_routing"]["handlers"]
        if entry["operation"] == "read_process_fixes"
    )
    bindings = yaml.safe_load(root.joinpath("overlay.inmemory.yaml").read_text())
    bindings["dependencies"][1]["config"]["process-items.json"] = json.dumps(
        [item.model_dump(mode="json") for item in process_input().items]
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(bindings))
    bind_overlay(load_overlay_bindings(overlay))
    bus = EventBusInmemory(environment="local", group="cause-idle-proof")
    answers = asyncio.Queue()
    await bus.start()

    async def collect(message):
        await answers.put(json.loads(message.value))

    try:
        await bus.subscribe(
            contract["terminal_event"],
            group_id="process-read-results",
            on_message=collect,
        )
        contracts = tuple(
            c for c in discover_contracts().contracts if c.name == contract["name"]
        )
        engine = MessageDispatchEngine()
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=contracts),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert all(row.outcome.value == "wired" for row in report.results)
        engine.freeze()
        request = ModelProcessFixReadRequest(
            request_id="read-runtime", now="2026-11-04T17:00:00Z"
        )
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=request.model_dump(mode="json"),
            correlation_id=correlation,
            event_type="omnimarket.process-fix-read-requested",
        )
        await bus.publish(
            operation["topic"], None, envelope.model_dump_json().encode(), None
        )
        answer = await asyncio.wait_for(answers.get(), timeout=5)
        assert answer["correlation_id"] == str(correlation)
        assert len(answer["payload"]["process_fixes"]["items"]) == 5
    finally:
        bind_overlay(None)
        await bus.close()
