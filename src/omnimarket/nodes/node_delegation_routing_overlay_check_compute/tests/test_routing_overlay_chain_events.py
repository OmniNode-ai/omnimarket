# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the routing overlay check (OMN-22976).

Publish the check command on an in-memory bus and assert the event it produces, under one
correlation id: the checked event with a WORKING verdict for the shipped documents, and the
failed event with a NOT WORKING verdict and the class named for a broken overlay. Every chain
assertion is shown red under a named mutation recorded in NAMED_MUTATIONS with the chain it
turns red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_delegation_routing_overlay_check_compute.handlers import (
    handler_delegation_routing_overlay_check as handler_module,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.handlers.handler_delegation_routing_overlay_check import (
    HandlerDelegationRoutingOverlayCheck,
    answer_command,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.tests.support import (
    bar_body,
    bars,
    class_names,
    contracts,
    load_topics,
    other_required_bar,
    overlay,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
CORRELATION = "overlay-chain-1"
BROKEN_CLASS = class_names()[0]

#: The named mutations, each with the chain assertion it turns red and the test that shows it.
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "drop-class-bar": (
        "error",
        "test_error_chain_a_class_without_a_bar_ends_on_the_failed_event",
    ),
    "edit-bar-keep-hash": (
        "error",
        "test_error_chain_a_bar_edited_without_a_new_hash_ends_on_the_failed_event",
    ),
    "failed-check-published-as-checked": (
        "error",
        "test_mutation_failed_check_published_as_checked_turns_the_error_chain_red",
    ),
    "failed-check-reports-working": (
        "error",
        "test_mutation_failed_check_reports_working_turns_the_error_chain_red",
    ),
    "checked-event-published-on-failed-topic": (
        "golden",
        "test_mutation_checked_event_on_failed_topic_turns_the_golden_chain_red",
    ),
    "dropped-correlation-id": (
        "golden",
        "test_mutation_dropped_correlation_id_turns_the_golden_chain_red",
    ),
}


def _command(contract_doc: dict[str, object], overlay_doc: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "correlation_id": CORRELATION,
            "task_class_contracts": contract_doc,
            "bars_overlay": overlay_doc,
        }
    ).encode()


async def _publish(payload: bytes) -> list[tuple[str, dict[str, Any]]]:
    handler = HandlerDelegationRoutingOverlayCheck()
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["overlay_check_requested"],
            on_message=on_command,
            group_id="overlay-check",
        )
        for role in ("overlay_checked", "overlay_check_failed"):
            await bus.subscribe(
                TOPICS[role],
                on_message=collector(TOPICS[role]),
                group_id=f"test-{role}",
            )
        await bus.publish(TOPICS["overlay_check_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, Any]]]) -> None:
    assert [topic for topic, _ in events] == [TOPICS["overlay_checked"]]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["verdict"] == "WORKING"
    assert body["findings"] == []
    assert body["checked_classes"] == len(class_names())


def assert_failed_chain(
    events: list[tuple[str, dict[str, Any]]], finding_class: str, task_class: str
) -> None:
    assert [topic for topic, _ in events] == [TOPICS["overlay_check_failed"]]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["verdict"] == "NOT WORKING"
    assert (task_class, finding_class) in {
        (f["task_class"], f["finding_class"]) for f in body["findings"]
    }


def _golden() -> list[tuple[str, dict[str, Any]]]:
    return asyncio.run(_publish(_command(contracts(), overlay())))


def _missing_bar() -> list[tuple[str, dict[str, Any]]]:
    doc = overlay()
    del bars(doc)[BROKEN_CLASS]
    return asyncio.run(_publish(_command(contracts(), doc)))


def _edited_bar() -> list[tuple[str, dict[str, Any]]]:
    doc = overlay()
    body = bar_body(doc, BROKEN_CLASS)
    body["required_bar"] = other_required_bar(float(str(body["required_bar"])))
    return asyncio.run(_publish(_command(contracts(), doc)))


# ── golden chain ─────────────────────────────────────────────────────────────


def test_golden_chain_the_shipped_overlay_ends_on_the_checked_event() -> None:
    assert_golden_chain(_golden())


# ── error chain ──────────────────────────────────────────────────────────────


def test_error_chain_a_class_without_a_bar_ends_on_the_failed_event() -> None:
    assert_failed_chain(_missing_bar(), "bar_missing", BROKEN_CLASS)


def test_error_chain_a_bar_edited_without_a_new_hash_ends_on_the_failed_event() -> None:
    assert_failed_chain(_edited_bar(), "hash_mismatch", BROKEN_CLASS)


def test_error_chain_a_payload_that_is_not_a_command_gets_a_typed_failure() -> None:
    events = asyncio.run(_publish(b'{"correlation_id": "x"}'))
    assert [topic for topic, _ in events] == [TOPICS["overlay_check_failed"]]
    assert events[0][1]["verdict"] == "NOT WORKING"
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


# ── named mutations ──────────────────────────────────────────────────────────


def test_mutation_failed_check_published_as_checked_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation failed-check-published-as-checked: a failing check ends on the success topic."""
    real = handler_module.events_of

    def mutated(result: Any, correlation_id: str, topics: dict[str, str]) -> Any:
        return [
            (topics["overlay_checked"], body)
            for _, body in real(result, correlation_id, topics)
        ]

    monkeypatch.setattr(handler_module, "events_of", mutated)
    with pytest.raises(AssertionError):
        assert_failed_chain(_missing_bar(), "bar_missing", BROKEN_CLASS)


def test_mutation_failed_check_reports_working_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation failed-check-reports-working: the failed event carries a WORKING verdict."""
    real = handler_module.events_of

    def mutated(result: Any, correlation_id: str, topics: dict[str, str]) -> Any:
        return [
            (topic, {**body, "verdict": "WORKING"})
            for topic, body in real(result, correlation_id, topics)
        ]

    monkeypatch.setattr(handler_module, "events_of", mutated)
    with pytest.raises(AssertionError):
        assert_failed_chain(_edited_bar(), "hash_mismatch", BROKEN_CLASS)


def test_mutation_checked_event_on_failed_topic_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation checked-event-published-on-failed-topic: a passing check ends on the failed topic."""
    real = handler_module.events_of

    def mutated(result: Any, correlation_id: str, topics: dict[str, str]) -> Any:
        return [
            (topics["overlay_check_failed"], body)
            for _, body in real(result, correlation_id, topics)
        ]

    monkeypatch.setattr(handler_module, "events_of", mutated)
    with pytest.raises(AssertionError):
        assert_golden_chain(_golden())


def test_mutation_dropped_correlation_id_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation dropped-correlation-id: the checked event no longer carries the request's id."""
    real = handler_module.events_of

    def mutated(result: Any, correlation_id: str, topics: dict[str, str]) -> Any:
        return real(result, "", topics)

    monkeypatch.setattr(handler_module, "events_of", mutated)
    with pytest.raises(AssertionError):
        assert_golden_chain(_golden())


def test_every_named_mutation_has_a_test_in_this_module() -> None:
    names = {
        name
        for name in globals()
        if name.startswith("test_") and callable(globals()[name])
    }
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden", "error"}


def test_contract_declares_the_exact_overlay_check_event_topics() -> None:
    assert TOPICS == {
        "overlay_check_requested": "onex.cmd.omnimarket.delegation-routing-overlay-check-requested.v1",
        "overlay_checked": "onex.evt.omnimarket.delegation-routing-overlay-checked.v1",
        "overlay_check_failed": "onex.evt.omnimarket.delegation-routing-overlay-check-failed.v1",
    }
