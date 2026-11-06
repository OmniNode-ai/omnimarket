# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay the item and accountability regressions through the public compute input."""

from __future__ import annotations

import hashlib
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_ask_state_compute as node
from omnimarket.models.model_ask_state import (
    ModelAskStateRequest,
    ModelRulingMechanismAudit,
)
from omnimarket.nodes.node_ask_state_compute.handlers.handler_ask_state import (
    HandlerAskState,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).with_name("fixtures")


def request(**updates: object) -> ModelAskStateRequest:
    data = json.loads((FIXTURES / "tracked_items.json").read_text())
    data["config"] = yaml.safe_load(
        Path(node.__file__).with_name("contract.yaml").read_text()
    )["config"]
    data.update(updates)
    return ModelAskStateRequest.model_validate(data)


def test_corpus_digest() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        raw = (FIXTURES / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected["sha256"]
        data = json.loads(raw)
        assert len(data["rows"]) == expected["rows"]
        assert len(data["prompts"]) == expected["prompts"]


def test_corpus_transport_overlays_are_declared() -> None:
    from omnimarket.handlers.bus_binding.node_event_bus_overlay import (
        read_node_event_bus,
    )

    memory = read_node_event_bus(FIXTURES / "overlays" / "inmemory.yaml")
    redpanda = read_node_event_bus(FIXTURES / "overlays" / "redpanda.yaml")
    assert memory is not None
    assert memory.type == "inmemory"
    assert memory.lane is None
    assert redpanda is not None
    assert redpanda.type == "kafka"
    assert redpanda.lane == "dev"


def test_fold_owner_state() -> None:
    report = HandlerAskState().handle(request())
    assert {"ask", "ruling", "promise", "question", "message", "unclassified"} <= {
        e.kind for e in report.asks
    }
    assert all(e.state and e.owner_lane for e in report.asks)
    assert {"owned", "done", "ticketed", "parked", "dropped", "new"} <= {
        e.state.value for e in report.asks
    }
    assert report.dropped
    assert all(
        e.orphaned == (e.state.value != "done" and e.running_lane is None)
        for e in report.asks
    )


def test_terminal_done_alone_retains_accountable_owner() -> None:
    report = HandlerAskState().handle(request())
    entry = next(e for e in report.asks if e.item_id == "cap-terminal")
    assert entry.state.value == "owned"
    assert entry.owner_lane == "builder"
    assert entry.running_lane is None
    assert entry.orphaned


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("example#42", "done"),
        ("https://github.com/example/project/pull/42", "done"),
        ("trust me", "owned"),
    ],
)
def test_completion_evidence_accepts_measured_merged_pr_references_and_resource_paths(
    evidence: str, expected: str
) -> None:
    original = request()
    claim, terminal = original.rows[:2]
    terminal += f" | evidence={evidence}"
    report = HandlerAskState().handle(
        original.model_copy(
            update={
                "rows": (claim, terminal),
                "merged_prs": ("example#42", "project#42"),
            }
        )
    )
    entry = next(e for e in report.asks if e.item_id == "cap-terminal")
    assert entry.state.value == expected
    assert entry.owner_lane == "builder"


def test_unclassified_prompt_is_an_owned_review_item() -> None:
    entry = next(
        e for e in HandlerAskState().handle(request()).asks if e.kind == "unclassified"
    )
    assert entry.owner_lane == "orchestrator"
    assert entry.words == "Thanks for the update."


def test_pending_ruling_age_comes_from_the_ruling() -> None:
    report = HandlerAskState().handle(request())
    ruling = next(e for e in report.asks if e.kind == "ruling" and e.said_at.hour == 11)
    assert ruling.state.value != "done"
    assert ruling.age_minutes == pytest.approx(
        (
            datetime(2026, 11, 4, 14, tzinfo=UTC)
            - datetime(2026, 11, 4, 11, 37, 59, tzinfo=UTC)
        ).total_seconds()
        / 60
    )


def test_a_ruling_does_not_count_itself_as_a_restatement() -> None:
    ruling = next(
        e for e in HandlerAskState().handle(request()).asks if e.kind == "ruling"
    )
    assert ruling.restated == 1


def test_fold_order_independent_with_distinct_stamps() -> None:
    original = request()
    rows = list(original.rows)
    random.Random(71).shuffle(rows)
    assert (
        HandlerAskState().handle(original).model_dump_json()
        == HandlerAskState()
        .handle(original.model_copy(update={"rows": tuple(rows)}))
        .model_dump_json()
    )


def test_same_stamp_uses_original_line_order() -> None:
    original = request()
    rows = (
        "2026-11-04T13:00:00Z | STATUS | lane=orch | for-item=ask-terminal | state=owned | owner=first",
        "2026-11-04T13:00:00Z | STATUS | lane=orch | for-item=ask-terminal | state=owned | owner=second",
    )
    for ordered, expected in ((rows, "second"), (tuple(reversed(rows)), "first")):
        report = HandlerAskState().handle(original.model_copy(update={"rows": ordered}))
        assert (
            next(e.owner_lane for e in report.asks if e.item_id == "cap-terminal")
            == expected
        )


def test_future_rows_do_not_change_a_replay() -> None:
    original = request()
    future = "2026-11-05T13:00:00Z | STATUS | lane=orch | for-item=ask-terminal | state=done | evidence=receipt:future"
    assert HandlerAskState().handle(original) == HandlerAskState().handle(
        original.model_copy(update={"rows": (*original.rows, future)})
    )


@pytest.mark.parametrize(
    ("state", "evidence", "closed"),
    [
        ("acting", ("receipt:acting-mechanism",), True),
        ("not_acting", ("receipt:installed",), False),
        ("acting", (), False),
    ],
)
def test_ruling_state_comes_from_the_mechanism_audit(state, evidence, closed) -> None:
    original = request()
    audit = ModelRulingMechanismAudit(
        ruling_stamp="2026-11-04T11:37:59Z",
        audited_at=original.now,
        state=state,
        evidence=evidence,
        owner_lane="mechanism-owner",
    )
    report = HandlerAskState().handle(
        original.model_copy(update={"ruling_mechanisms": (audit,)})
    )
    ruling = next(e for e in report.asks if e.kind == "ruling")
    assert (ruling.state.value == "done") == closed
    assert ruling.owner_lane == "mechanism-owner"


def test_captured_ruling_does_not_close_from_a_terminal_receipt() -> None:
    original = request()
    prompt = original.prompts[0]
    prompt = prompt.model_copy(
        update={"items": (prompt.items[0].model_copy(update={"kind": "ruling"}),)}
    )
    rows = (
        *original.rows[:2],
        "2026-11-04T10:10:00Z | STATUS | lane=builder | for-item=ask-terminal | state=done | evidence=receipt:lane-finished",
    )
    report = HandlerAskState().handle(
        original.model_copy(update={"prompts": (prompt,), "rows": rows})
    )
    assert (
        next(e for e in report.asks if e.item_id == "cap-terminal").state.value
        != "done"
    )


def test_an_old_terminal_does_not_end_a_newer_live_claim() -> None:
    original = request()
    rows = (
        "2026-11-04T10:01:00Z | CLAIM | lane=builder | for-item=ask-terminal",
        "2026-11-04T10:02:00Z | CLAIM | lane=builder | for-item=ask-terminal",
        "2026-11-04T10:03:00Z | TERMINAL | lane=builder | closes-CLAIM=2026-11-04T10:01:00Z | outcome=blocked",
    )
    report = HandlerAskState().handle(original.model_copy(update={"rows": rows}))
    entry = next(e for e in report.asks if e.item_id == "cap-terminal")
    assert entry.running_lane == "builder"
    assert entry.state.value == "owned"


@pytest.mark.parametrize(
    ("recipient", "expected"),
    [("operator", "done"), ("landing-controller", "ticketed")],
)
def test_answer_reference_closes_a_question_only_when_delivered_to_operator(
    recipient: str, expected: str
) -> None:
    original = request()
    reply = f"2026-11-04T13:00:00Z | MSG | from=orchestrator | to={recipient} | answers=cap-ticketed | The result is recorded."
    report = HandlerAskState().handle(
        original.model_copy(update={"rows": (*original.rows, reply)})
    )
    entry = next(e for e in report.asks if e.item_id == "cap-ticketed")
    assert entry.state.value == expected
    assert entry.owner_lane == "reviewer"


@pytest.mark.parametrize(
    "node_name", ["node_ask_state_compute", "node_operator_message_state_compute"]
)
async def test_runtime_fold_uses_discovered_contract_and_typed_payload(
    node_name: str,
) -> None:
    """Exercise installed discovery, DI and terminal publication, using the real in-memory bus."""
    import asyncio
    from importlib.resources import files
    from uuid import uuid4

    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    contract = yaml.safe_load(
        files("omnimarket.nodes." + node_name).joinpath("contract.yaml").read_text()
    )
    discovered = tuple(
        c for c in discover_contracts().contracts if c.name == contract["name"]
    )
    assert len(discovered) == 1
    result_queue = asyncio.Queue()
    bus = EventBusInmemory(environment="local", group="tracked-item-replay")
    await bus.start()

    async def collect(message):
        await result_queue.put(json.loads(message.value))

    try:
        await bus.subscribe(
            contract["terminal_event"],
            group_id="tracked-item-results",
            on_message=collect,
        )
        engine = MessageDispatchEngine()
        wiring = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=discovered),
            engine,
            event_bus=bus,
            environment="local",
        )
        assert [r.outcome.value for r in wiring.results] == ["wired"]
        engine.freeze()
        if node_name == "node_ask_state_compute":
            original = request()
            expected = HandlerAskState().handle(original).model_dump(mode="json")
        else:
            from omnimarket.models.model_operator_message_state import (
                ModelOperatorMessageStateRequest,
            )
            from omnimarket.nodes.node_operator_message_state_compute.node import (
                NodeOperatorMessageStateCompute,
            )

            original = ModelOperatorMessageStateRequest(
                rows=(
                    "2026-11-04T12:00:00Z | STATUS | kind=operator-message | capture=arrival | lane=session-a",
                ),
                now=request().now,
                config=contract["config"],
            )
            expected = (
                NodeOperatorMessageStateCompute()
                .handle(original)
                .model_dump(mode="json")
            )
        correlation = uuid4()
        envelope = ModelEventEnvelope[object](
            payload=original.model_dump(mode="json"),
            correlation_id=correlation,
            event_type="omnimarket.ask-state-requested",
        )
        await bus.publish(
            contract["runtime_dispatch"]["command_topic"],
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        result = await asyncio.wait_for(result_queue.get(), timeout=5)
        assert result["correlation_id"] == str(correlation)
        assert result["payload"] == expected
    finally:
        await bus.close()
