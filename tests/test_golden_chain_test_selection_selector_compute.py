# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of node_test_selection_selector_compute over the in-memory bus (OMN-23296)."""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_test_selection_selector_compute.adapters.policy_file import (
    load_policy,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    EnumSelectorOutcome,
    ModelSelectorDecision,
    ModelSelectorFailed,
    ModelSelectorRequest,
)

pytestmark = pytest.mark.unit


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_test_selection_selector_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return document


def _request() -> bytes:
    return (
        ModelSelectorRequest(selected_paths=("tests/a/",), repo_root="/unused")
        .model_dump_json()
        .encode()
    )


async def _run_chain(
    handler: HandlerTestSelectionSelector, payload: bytes
) -> list[tuple[str, bytes]]:
    """Publish ``payload`` on the contract's command topic; return every event the node published."""
    contract = _contract()
    spec = contract["handler"]
    assert getattr(importlib.import_module(spec["module"]), spec["class"]) is type(
        handler
    )
    request_topic = contract["runtime_dispatch"]["command_topic"]
    terminal_topic = contract["terminal_event"]
    failed_topic = contract["runtime_dispatch"]["terminal_events"]["failure"]
    assert contract["event_bus"]["subscribe_topics"] == [request_topic]
    assert contract["event_bus"]["publish_topics"] == [terminal_topic, failed_topic]
    assert request_topic == "onex.cmd.omnimarket.test-selection-select-requested.v1"
    assert terminal_topic == "onex.evt.omnimarket.test-selection-selected.v1"

    bus = EventBusInmemory()
    seen: list[tuple[str, bytes]] = []
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handle(message: Any) -> None:
        try:
            request = ModelSelectorRequest.model_validate_json(message.value)
        except ValidationError as exc:
            failure = ModelSelectorFailed(reason=type(exc).__name__)
            await bus.publish(
                failed_topic, key=None, value=failure.model_dump_json().encode()
            )
            return
        await bus.publish(
            terminal_topic,
            key=None,
            value=handler.handle(request).model_dump_json().encode(),
        )

    def collector(topic: str) -> Any:
        async def collect(message: Any) -> None:
            seen.append((topic, message.value))
            if not done.done():
                done.set_result(None)

        return collect

    await bus.start()
    try:
        await bus.subscribe(request_topic, on_message=handle, group_id="tss")
        for topic in (terminal_topic, failed_topic):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        await bus.publish(request_topic, key=None, value=payload)
        await asyncio.wait_for(done, timeout=5)
    finally:
        await bus.close()
    return seen


def _handler(durations: dict[str, float] | None) -> HandlerTestSelectionSelector:
    return HandlerTestSelectionSelector(SourceInMemory(durations), load_policy())


@pytest.mark.asyncio
async def test_golden_chain_narrowed_selection_ends_in_the_terminal_event() -> None:
    seen = await _run_chain(_handler({"tests/a/test_x.py": 700.0}), _request())

    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    decision = ModelSelectorDecision.model_validate_json(seen[0][1])
    assert decision.outcome is EnumSelectorOutcome.NARROWED
    assert decision.split_count == 2
    assert decision.estimated_seconds == 700.0


@pytest.mark.asyncio
async def test_error_chain_too_heavy_selection_publishes_an_escalation() -> None:
    seen = await _run_chain(_handler({"tests/a/test_x.py": 4100.0}), _request())

    assert [t for t, _ in seen] == [_contract()["terminal_event"]]
    decision = ModelSelectorDecision.model_validate_json(seen[0][1])
    assert decision.outcome is EnumSelectorOutcome.FULL_SUITE_TOO_HEAVY
    assert decision.split_count is None


@pytest.mark.asyncio
async def test_error_chain_unreadable_durations_publish_an_escalation() -> None:
    seen = await _run_chain(_handler(None), _request())

    decision = ModelSelectorDecision.model_validate_json(seen[0][1])
    assert decision.outcome is EnumSelectorOutcome.FULL_SUITE_DURATIONS_UNREAD


@pytest.mark.asyncio
async def test_error_chain_bad_payload_publishes_the_typed_failure_event() -> None:
    seen = await _run_chain(_handler({}), b'{"selected_paths": []}')

    failed_topic = _contract()["runtime_dispatch"]["terminal_events"]["failure"]
    assert [t for t, _ in seen] == [failed_topic]
    failure = ModelSelectorFailed.model_validate_json(seen[0][1])
    assert failure.reason == "ValidationError"
    assert failure.terminal_failure_cause == "ValidationError"
