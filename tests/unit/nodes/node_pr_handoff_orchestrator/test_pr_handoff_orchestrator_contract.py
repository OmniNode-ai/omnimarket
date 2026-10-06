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
    ModelPrHandoffHandedOff,
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
    # An immediate redelivery re-emits what the leg emitted and writes nothing.
    assert await handler.handle(b.request(cid, requested_at=b.at(110))) == first
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


def _answer(
    command: ModelPrHandoffLedgerAppendCommand,
    status: EnumPrHandoffLedgerStatus,
    attempt: int,
) -> ModelPrHandoffLedgerAppended:
    return ModelPrHandoffLedgerAppended(
        correlation_id=command.correlation_id,
        handoff_key=command.handoff_key,
        ledger_request_id=command.ledger_request_id,
        attempt=attempt,
        status=status,
        answered_at=b.at(200 + attempt),
    )


def _kinds(events: list[Any]) -> list[str]:
    return [type(e).__name__ for e in events]


async def test_a_replayed_superseded_request_is_dropped_not_restarted() -> None:
    """Codex review 5: A waits, newer B supersedes it, a replay of A changes nothing."""
    first, second = b.new_cid(), b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(first, requested_at=b.at(0)))
    superseding = await handler.handle(b.request(second, requested_at=b.at(10)))
    assert _kinds(superseding) == ["ModelPrHandoffFailed", "ModelPrHandoffAccepted"]
    await handler.handle(b.observation(b.at(-5), draft=True))
    assert await handler.handle(b.request(first, requested_at=b.at(0))) == []


async def test_a_request_older_than_the_one_in_flight_loses() -> None:
    first, older = b.new_cid(), b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(first, requested_at=b.at(10)))
    (failed,) = await handler.handle(b.request(older, requested_at=b.at(0)))
    assert isinstance(failed, ModelPrHandoffFailed)
    assert failed.correlation_id == older
    assert failed.error_code is EnumPrHandoffErrorCode.SUPERSEDED
    # Redelivered at once, it is re-emitted; replayed after another leg, it is dropped.
    assert await handler.handle(b.request(older, requested_at=b.at(0))) == [failed]
    await handler.handle(b.observation(b.at(20), draft=True))
    assert await handler.handle(b.request(older, requested_at=b.at(0))) == []


async def test_a_request_refused_while_appending_stays_refused() -> None:
    """Codex review 6: the refusal is recorded, so a replay after the append is dropped."""
    first, second = b.new_cid(), b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.observation(b.at(-5)))
    (*_, command) = await handler.handle(b.request(first))
    assert isinstance(command, ModelPrHandoffLedgerAppendCommand)
    refused = await handler.handle(b.request(second, requested_at=b.at(5)))
    assert _kinds(refused) == ["ModelPrHandoffFailed"]
    done = await handler.handle(_answer(command, EnumPrHandoffLedgerStatus.ACCEPTED, 1))
    assert _kinds(done) == ["ModelPrHandoffHandedOff"]
    assert await handler.handle(b.request(second, requested_at=b.at(5))) == []


async def test_the_deadline_is_checked_before_readiness() -> None:
    """Codex review 8: a ready observation after the deadline times out, it does not append."""
    cid = b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(cid, wait_budget_s=60))
    (failed,) = await handler.handle(b.observation(b.at(61)))
    assert isinstance(failed, ModelPrHandoffFailed)
    assert failed.error_code is EnumPrHandoffErrorCode.TIMED_OUT


async def test_a_newer_request_after_the_deadline_times_the_old_one_out() -> None:
    first, second = b.new_cid(), b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(first, wait_budget_s=60))
    (failed, accepted) = await handler.handle(b.request(second, requested_at=b.at(90)))
    assert isinstance(failed, ModelPrHandoffFailed)
    assert failed.error_code is EnumPrHandoffErrorCode.TIMED_OUT
    assert type(accepted).__name__ == "ModelPrHandoffAccepted"


async def test_an_earlier_attempts_success_hands_off() -> None:
    """Codex review 9: pending on attempt 1, resend, then attempt 1's late success counts."""
    cid = b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.observation(b.at(-5)))
    (*_, command) = await handler.handle(b.request(cid))
    assert isinstance(command, ModelPrHandoffLedgerAppendCommand)
    resend = await handler.handle(
        _answer(command, EnumPrHandoffLedgerStatus.PENDING, 1)
    )
    assert _kinds(resend) == ["ModelPrHandoffLedgerAppendCommand"]
    late = await handler.handle(_answer(command, EnumPrHandoffLedgerStatus.ACCEPTED, 1))
    assert _kinds(late) == ["ModelPrHandoffHandedOff"]
    stale = await handler.handle(_answer(command, EnumPrHandoffLedgerStatus.PENDING, 2))
    assert stale == []


async def test_a_lost_command_is_re_emitted_by_the_observation_redelivery() -> None:
    """Codex review 7: the row was written but the publish was lost; redelivery recovers it."""
    cid = b.new_cid()
    handler = HandlerPrHandoffOrchestrator(store=InMemoryPrHandoffRowStore())
    await handler.handle(b.request(cid))
    observed = b.observation(b.at(30))
    first = await handler.handle(observed)
    assert _kinds(first) == ["ModelPrHandoffLedgerAppendCommand"]
    assert await handler.handle(observed) == first
    command = first[0]
    assert isinstance(command, ModelPrHandoffLedgerAppendCommand)
    done = await handler.handle(
        _answer(command, EnumPrHandoffLedgerStatus.DUPLICATE, 1)
    )
    assert isinstance(done[0], ModelPrHandoffHandedOff)
