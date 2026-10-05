# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_orchestrator: the code is the contract, and the drops drop (OMN-20636)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events import topics
from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffLedgerStatus,
    EnumPrHandoffState,
    ModelPrHandoffFailed,
    ModelPrHandoffLedgerAppendCommand,
    ModelPrHandoffLedgerAppended,
)
from omnimarket.models.pr_handoff.enum_pr_handoff_error_code import (
    DECISION_REFUSAL_CODES,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.event_topics import (
    PR_HANDOFF_EVENT_TOPICS,
    publish_topic_for,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.handlers import (
    HandlerPrHandoffOrchestrator,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.core import (
    TRANSITIONS,
    check_transition,
    ledger_request_id_for,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
    decode_row,
    encode_row,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_pr_handoff_orchestrator"
)
S = EnumPrHandoffState


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_transitions_are_the_contract_state_machine() -> None:
    machine = _contract()["state_machine"]
    declared = {
        (t["from_state"], t["trigger"]): t["to_state"] for t in machine["transitions"]
    }
    assert declared == dict(TRANSITIONS)
    names = [s["state_name"] for s in machine["states"]]
    assert names == [
        "REQUESTED",
        "WAITING",
        "APPENDING",
        "HANDED_OFF",
        "REFUSED",
        "TIMED_OUT",
    ]
    assert names == [s.value for s in EnumPrHandoffState]
    assert machine["terminal_states"] == ["HANDED_OFF", "REFUSED", "TIMED_OUT"]
    assert machine["error_states"] == ["REFUSED", "TIMED_OUT"]
    assert machine["initial_state"] == "REQUESTED"


def test_every_decision_refusal_has_its_own_trigger() -> None:
    for code in DECISION_REFUSAL_CODES:
        assert check_transition(S.WAITING, f"evaluated_{code.value}") is S.REFUSED
    with pytest.raises(ValueError, match="no transition"):
        check_transition(S.HANDED_OFF, "evaluated_ready")


def test_published_events_are_the_event_topic_map() -> None:
    contract = _contract()
    published = {e["event_type"]: e["topic"] for e in contract["published_events"]}
    assert published == {
        cls.__name__.removeprefix("Model"): topic
        for cls, topic in PR_HANDOFF_EVENT_TOPICS.items()
    }
    assert set(contract["event_bus"]["publish_topics"]) == set(published.values())
    assert set(contract["event_bus"]["subscribe_topics"]) == {
        topics.PR_HANDOFF_REQUESTED_TOPIC_V1,
        topics.PR_STATE_OBSERVED_TOPIC_V1,
        topics.PR_HANDOFF_LEDGER_APPENDED_TOPIC_V1,
    }
    with pytest.raises(ValueError, match="not an emission"):
        publish_topic_for(b.request(b.new_cid()))


def test_state_io_key_is_derived_on_every_ingress() -> None:
    # The row key the state_io block will name (OMN-20638); rows are in process today.
    assert "state_io" not in _contract()
    cid = b.new_cid()
    assert b.request(cid).handoff_key == b.KEY
    assert b.observation(b.at(0)).handoff_key == b.KEY
    with pytest.raises(ValueError, match="does not match"):
        b.request(cid, handoff_key="omnimarket#1")


async def test_row_codec_round_trip_and_well_known_keys() -> None:
    cid = b.new_cid()
    run = await b.drive([], [(b.request(cid), cid)])
    row, _version = await run.store.load(b.KEY)
    assert row is not None
    raw = encode_row(row)
    payload = json.loads(raw)
    assert payload["state"] == "WAITING"
    assert payload["in_flight"] is False
    assert payload["tenant_id"] == "platform"
    assert decode_row(raw) == row


async def test_runtime_abandoned_append_is_ended_by_the_next_request() -> None:
    first, second = b.new_cid(), b.new_cid()
    store = InMemoryPrHandoffRowStore()
    handler = HandlerPrHandoffOrchestrator(store=store)
    await handler.handle(b.observation(b.at(-10)))
    emitted = await handler.handle(b.request(first))
    assert isinstance(emitted[-1], ModelPrHandoffLedgerAppendCommand)
    row, _ = await store.load(b.KEY)
    assert row is not None
    payload = json.loads(encode_row(row))
    payload["failure_reason"] = "recover_stale_rows gave the row up"
    recovered = decode_row(json.dumps(payload))
    assert recovered.episode is not None
    assert recovered.episode.append_abandoned
    # Without the give-up note, a second request is refused and the row is untouched.
    refused = await handler.handle(b.request(second, requested_at=b.at(30)))
    assert [type(e).__name__ for e in refused] == ["ModelPrHandoffFailed"]
    assert isinstance(refused[0], ModelPrHandoffFailed)
    assert refused[0].correlation_id == second
    assert refused[0].error_code is EnumPrHandoffErrorCode.INVALID_REQUEST
    # With it, the second request ends the abandoned append and is handed off itself.
    await store.compare_and_set(b.KEY, store.version(b.KEY), recovered)
    third = b.new_cid()
    ended = await handler.handle(b.request(third, requested_at=b.at(40)))
    kinds = [type(e).__name__ for e in ended]
    assert kinds == [
        "ModelPrHandoffFailed",
        "ModelPrHandoffAccepted",
        "ModelPrHandoffLedgerAppendCommand",
    ]
    assert isinstance(ended[0], ModelPrHandoffFailed)
    assert ended[0].correlation_id == first
    assert ended[0].error_code is EnumPrHandoffErrorCode.APPEND_UNCONFIRMED


async def test_instances_without_a_store_share_the_process_rows() -> None:
    """The runtime may build one instance per route; a request and its observation must meet."""
    cid = b.new_cid()
    request_route, observation_route = (
        HandlerPrHandoffOrchestrator(),
        HandlerPrHandoffOrchestrator(),
    )
    accepted = await request_route.handle(b.request(cid, repo="omniweb", pr_number=777))
    assert [type(e).__name__ for e in accepted] == ["ModelPrHandoffAccepted"]
    decided = await observation_route.handle(
        b.observation(
            b.at(60), repo="omniweb", pr_number=777, title="feat(OMN-20636): x"
        )
    )
    assert [type(e).__name__ for e in decided] == ["ModelPrHandoffLedgerAppendCommand"]


async def test_an_observation_older_than_the_request_is_decided_at_the_request_time() -> (
    None
):
    cid = b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(cid, requested_at=b.at(600)))
    (command,) = await handler.handle(b.observation(b.at(10)))
    assert isinstance(command, ModelPrHandoffLedgerAppendCommand)
    assert command.requested_at == b.at(600)
    assert command.rows.startswith(f"{b.stamp(b.at(600))} | MSG | ")


async def test_drops() -> None:
    cid = b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.observation(b.at(100)))
    assert await handler.handle(b.observation(b.at(50), draft=True)) == []
    first = await handler.handle(b.request(cid, requested_at=b.at(110)))
    assert [type(e).__name__ for e in first][:1] == ["ModelPrHandoffAccepted"]
    assert await handler.handle(b.request(cid, requested_at=b.at(110))) == []
    command = first[-1]
    assert isinstance(command, ModelPrHandoffLedgerAppendCommand)
    assert command.ledger_request_id == ledger_request_id_for(cid)
    wrong_attempt = ModelPrHandoffLedgerAppended(
        correlation_id=cid,
        handoff_key=b.KEY,
        ledger_request_id=command.ledger_request_id,
        attempt=2,
        status=EnumPrHandoffLedgerStatus.ACCEPTED,
        answered_at=b.at(120),
    )
    assert await handler.handle(wrong_attempt) == []
