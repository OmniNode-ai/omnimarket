# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of node_public_consumer_check_compute over the in-memory bus (OMN-23145)."""

from __future__ import annotations

import asyncio
import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_public_consumer_check_compute.adapters.source_in_memory import (
    SourceInMemory,
)
from omnimarket.nodes.node_public_consumer_check_compute.handlers.handler_public_consumer_check import (
    HandlerPublicConsumerCheck,
)
from omnimarket.nodes.node_public_consumer_check_compute.models.model_public_consumer_check import (
    EnumBatchDecision,
    EnumEntityDisposition,
    EnumScopeKind,
    ModelEntity,
    ModelPublicConsumerCheckFailed,
    ModelPublicConsumerCheckRequest,
    ModelPublicConsumerCheckResult,
    ModelSearchScope,
)

pytestmark = pytest.mark.unit

REPO = ModelSearchScope(kind=EnumScopeKind.PUBLIC_REPOSITORY, name="pub-repo")
PACKAGE = ModelSearchScope(kind=EnumScopeKind.PUBLISHED_PACKAGE, name="pub-package")
SCOPES = (REPO, PACKAGE)
REPO_KEY = (REPO.kind.value, REPO.name)
PACKAGE_KEY = (PACKAGE.kind.value, PACKAGE.name)


def _source(unreadable: frozenset[tuple[str, str]] = frozenset()) -> SourceInMemory:
    return SourceInMemory({REPO_KEY: {}, PACKAGE_KEY: {}}, unreadable)


def _request() -> ModelPublicConsumerCheckRequest:
    return ModelPublicConsumerCheckRequest(
        batch_id="batch-1",
        repository="origin-repo",
        entities=(ModelEntity(entity_id="e1", references=("pkg.old_mod",)),),
    )


def _contract() -> dict[str, Any]:
    document: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_public_consumer_check_compute")
        .joinpath("contract.yaml")
        .read_text()
    )
    return document


async def _run_chain(
    handler: HandlerPublicConsumerCheck, payload: bytes
) -> tuple[list[tuple[str, bytes]], dict[str, Any]]:
    """Publish ``payload`` on the contract's command topic; return every event the node published."""
    contract = _contract()
    spec = contract["handler"]
    assert getattr(importlib.import_module(spec["module"]), spec["class"]) is type(
        handler
    )
    request_topic = contract["runtime_dispatch"]["command_topic"]
    published = contract["event_bus"]["publish_topics"]
    terminal_topic = contract["terminal_event"]
    failed_topic = next(t for t in published if t.endswith("failed.v1"))
    assert contract["event_bus"]["subscribe_topics"] == [request_topic]
    assert request_topic == "onex.cmd.omnimarket.public-consumer-check-requested.v1"
    assert terminal_topic == "onex.evt.omnimarket.public-consumer-checked.v1"
    assert failed_topic == "onex.evt.omnimarket.public-consumer-check-failed.v1"

    bus = EventBusInmemory()
    seen: list[tuple[str, bytes]] = []
    done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def handle(message: Any) -> None:
        try:
            request = ModelPublicConsumerCheckRequest.model_validate_json(message.value)
        except ValidationError as exc:
            failure = ModelPublicConsumerCheckFailed(
                batch_id="unreadable", reason=f"{type(exc).__name__}"
            )
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
        await bus.subscribe(request_topic, on_message=handle, group_id="pcc")
        for topic in (terminal_topic, failed_topic):
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        await bus.publish(request_topic, key=None, value=payload)
        await asyncio.wait_for(done, timeout=5)
    finally:
        await bus.close()
    return seen, {"terminal": terminal_topic, "failed": failed_topic}


@pytest.mark.asyncio
async def test_golden_chain_clean_batch_ends_in_the_terminal_event() -> None:
    handler = HandlerPublicConsumerCheck(_source(), SCOPES)
    seen, topics = await _run_chain(handler, _request().model_dump_json().encode())

    assert [t for t, _ in seen] == [topics["terminal"]]
    result = ModelPublicConsumerCheckResult.model_validate_json(seen[0][1])
    assert result.decision is EnumBatchDecision.ACCEPTED
    assert result.batch_id == "batch-1"
    assert [r.read for r in result.verdicts[0].evidence.scopes] == [True, True]


@pytest.mark.asyncio
async def test_error_chain_unreadable_scope_publishes_a_refused_batch() -> None:
    handler = HandlerPublicConsumerCheck(
        _source(unreadable=frozenset({REPO_KEY})), SCOPES
    )
    seen, topics = await _run_chain(handler, _request().model_dump_json().encode())

    assert [t for t, _ in seen] == [topics["terminal"]]
    result = ModelPublicConsumerCheckResult.model_validate_json(seen[0][1])
    assert result.decision is EnumBatchDecision.REFUSED
    assert result.verdicts[0].disposition is EnumEntityDisposition.KEPT_SEARCH_UNREAD
    assert any("unread" in reason for reason in result.verdicts[0].reasons)


@pytest.mark.asyncio
async def test_error_chain_bad_payload_publishes_the_typed_failure_event() -> None:
    handler = HandlerPublicConsumerCheck(_source(), SCOPES)
    seen, topics = await _run_chain(handler, b'{"batch_id": "b", "entities": []}')

    assert [t for t, _ in seen] == [topics["failed"]]
    failure = ModelPublicConsumerCheckFailed.model_validate_json(seen[0][1])
    assert failure.reason == "ValidationError"
