# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fake ports and the two-profile harness of the agent review dispatch tests.

Addresses are RFC 5737 documentation addresses, model names are test names. The overlay
the node reads is written under ``tmp_path`` and selected by ``ONEX_SKILL_OVERLAY_ROOTS``.
The ``inmemory`` profile calls the handler directly; the ``redpanda_overlay`` profile
selects the Redpanda transport in the overlay and carries the command and the terminal
over the contract's topics.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_agent_review_dispatch import (
    HandlerAgentReviewDispatch,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    ModelAgentFinding,
    ModelAgentReceipt,
    ModelAgentReceiptCandidate,
    ModelAgentReviewConfig,
    ModelAgentReviewRequest,
    ModelAgentReviewTerminal,
    ModelAgentVoter,
    ModelAgentVoterReview,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.overlay import (
    load_agent_review_config,
    load_topics,
)
from tests.node_overlay_support import install_node_overlay

NODE = "node_agent_review_dispatch_orchestrator"
REPO = "OmniNode-ai/example"
PR = 7
HEAD = "a" * 40
MOVED = "b" * 40
KEY = f"{REPO}#{PR}#{HEAD}"

OVERLAY: dict[str, Any] = {
    "transport": "in_memory",
    "overlay_revision": "test-revision-1",
    "reviewer_id": "test-reviewer",
    "receipt_fence": "test-fence",
    "deadline_seconds": 5.0,
    "lab_call_timeout_seconds": 2.0,
    "post_attempts": 3,
    "roster": [
        {
            "identity": "voter-one",
            "provider": "lab-local",
            "rung": "lab",
            "endpoint_class": "lab_local",
            "endpoint": "192.0.2.11:8000",
            "model": "model-a",
            "route_key": "route-one",
        },
        {
            "identity": "voter-two",
            "provider": "lab-local",
            "rung": "lab",
            "endpoint_class": "lab_local",
            "endpoint": "192.0.2.12:8000",
            "model": "model-a",
            "route_key": "route-two",
        },
    ],
    "policy": {
        "approved_identities": ["voter-one", "voter-two"],
        "approved_rungs": ["lab"],
        "forbidden_providers": ["anthropic"],
        "forbidden_endpoint_classes": ["review_gateway"],
        "forbidden_endpoints": [],
        "forbidden_hosts": [],
        "publisher_identity": "test-publisher",
    },
    "quorum": {
        "quorum_size": 2,
        "min_agreeing_voters": 2,
        "line_proximity_lines": 3,
        "blocking_severities": ["critical", "error"],
    },
}


class SimulatedCrash(BaseException):
    """A process death: not an ``Exception``, so no handler turns it into a terminal."""


@dataclass
class World:
    """Every fake port's state, and the counts the tests read."""

    heads: list[str] = field(default_factory=lambda: [HEAD])
    head_reads: int = 0
    lab_calls: list[str] = field(default_factory=list)
    lab_findings: dict[str, tuple[ModelAgentFinding, ...]] = field(default_factory=dict)
    lab_empty_event: set[str] = field(default_factory=set)
    lab_raises: set[str] = field(default_factory=set)
    lab_delay: float = 0.0
    lab_gate: asyncio.Event | None = None
    lab_arrivals: int = 0
    sign_calls: int = 0
    stored: dict[str, ModelAgentReceipt] = field(default_factory=dict)
    store_writes: int = 0
    posts: list[str] = field(default_factory=list)
    find_calls: int = 0
    terminals: dict[str, ModelAgentReviewTerminal] = field(default_factory=dict)
    nonces: int = 0
    crash_at: str | None = None
    crash_armed: bool = True
    post_commits_then_times_out: bool = False

    def total_calls(self) -> int:
        return (
            self.head_reads
            + len(self.lab_calls)
            + self.sign_calls
            + self.store_writes
            + len(self.posts)
            + self.find_calls
        )

    def crash(self, point: str) -> None:
        if self.crash_at == point and self.crash_armed:
            self.crash_armed = False
            raise SimulatedCrash(point)


class FakeHead:
    def __init__(self, world: World) -> None:
        self.world = world

    async def read_head(self, repo: str, pr: int) -> str:
        reads = self.world.head_reads
        self.world.head_reads += 1
        heads = self.world.heads
        return heads[min(reads, len(heads) - 1)]


class FakeLab:
    def __init__(self, world: World) -> None:
        self.world = world

    async def review(
        self, voter: ModelAgentVoter, repo: str, pr: int, head_sha: str
    ) -> ModelAgentVoterReview:
        world = self.world
        world.crash("before_lab_review")
        world.lab_calls.append(voter.identity)
        world.lab_arrivals += 1
        if world.lab_gate is not None:
            if world.lab_arrivals >= 2:
                world.lab_gate.set()
            await world.lab_gate.wait()
        if world.lab_delay:
            await asyncio.sleep(world.lab_delay)
        if voter.identity in world.lab_raises:
            raise ConnectionError("lab endpoint unreachable")
        return ModelAgentVoterReview(
            identity=voter.identity,
            endpoint=voter.endpoint,
            model=voter.model,
            delegation_event_id=""
            if voter.identity in world.lab_empty_event
            else f"event-{voter.identity}",
            findings=world.lab_findings.get(voter.identity, ()),
        )


class FakeSigner:
    def __init__(self, world: World) -> None:
        self.world = world

    async def sign(self, candidate: ModelAgentReceiptCandidate) -> ModelAgentReceipt:
        self.world.sign_calls += 1
        digest = hashlib.sha256(candidate.model_dump_json().encode()).hexdigest()
        receipt = ModelAgentReceipt(**candidate.model_dump(), signature=digest)
        self.world.crash("after_sign_before_store")
        return receipt


class FakeStore:
    def __init__(self, world: World) -> None:
        self.world = world
        self.lock = asyncio.Lock()

    async def get(self, key: str) -> ModelAgentReceipt | None:
        return self.world.stored.get(key)

    async def insert_if_absent(
        self, key: str, receipt: ModelAgentReceipt
    ) -> ModelAgentReceipt:
        async with self.lock:
            held = self.world.stored.get(key)
            if held is None:
                self.world.stored[key] = receipt
                self.world.store_writes += 1
                held = receipt
        self.world.crash("after_store_before_post")
        return held


class FakePoster:
    def __init__(self, world: World) -> None:
        self.world = world
        self.attempts = 0

    async def find_posted(
        self, repo: str, pr: int, publisher_identity: str, receipt_id: str
    ) -> bool:
        self.world.find_calls += 1
        return receipt_id in self.world.posts

    async def post(
        self, repo: str, pr: int, head_sha: str, receipt: ModelAgentReceipt, fence: str
    ) -> None:
        self.attempts += 1
        self.world.posts.append(receipt.id)
        if self.world.post_commits_then_times_out and self.attempts == 1:
            raise TimeoutError("the post committed and the answer was lost")
        self.world.crash("after_post")


class FakeTerminalLog:
    def __init__(self, world: World) -> None:
        self.world = world

    async def get(self, command_id: str) -> ModelAgentReviewTerminal | None:
        return self.world.terminals.get(command_id)

    async def record(self, terminal: ModelAgentReviewTerminal) -> None:
        self.world.terminals[terminal.command_id] = terminal


class FakeClock:
    def __init__(self, world: World) -> None:
        self.world = world

    def now(self) -> datetime:
        return datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC)

    def new_id(self) -> str:
        self.world.nonces += 1
        return f"receipt-{self.world.nonces}"


class InMemoryBus:
    """A topic is a list of subscribers; publishing calls them in order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="agent-review-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


@dataclass
class Harness:
    profile: str
    config: ModelAgentReviewConfig
    world: World
    handler: HandlerAgentReviewDispatch

    def request(
        self, command_id: str = "cmd-1", head: str = HEAD
    ) -> ModelAgentReviewRequest:
        return ModelAgentReviewRequest(
            command_id=command_id, repo=REPO, pr=PR, head_sha=head
        )

    async def run(self, request: ModelAgentReviewRequest) -> ModelAgentReviewTerminal:
        if self.profile == "inmemory":
            return await self.handler.handle(request)
        topics = load_topics()
        bus = InMemoryBus()
        seen: asyncio.Queue[ModelAgentReviewTerminal] = asyncio.Queue()

        async def on_command(message: ModelEventMessage) -> None:
            terminal = await self.handler.handle(
                ModelAgentReviewRequest.model_validate_json(message.value)
            )
            await bus.publish(
                topics.terminal_topic(terminal.outcome),
                None,
                terminal.model_dump_json().encode(),
            )

        async def observe(message: ModelEventMessage) -> None:
            await seen.put(ModelAgentReviewTerminal.model_validate_json(message.value))

        stops = [
            await bus.subscribe_ungrouped(topic, observe)
            for topic in (topics.completed, topics.refused, topics.failed)
        ]
        stops.append(await bus.subscribe_ungrouped(topics.command, on_command))
        try:
            await bus.publish(topics.command, None, request.model_dump_json().encode())
            return await asyncio.wait_for(seen.get(), timeout=30)
        finally:
            for stop in stops:
                await stop()


def build_harness(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    overlay: dict[str, Any] | None = None,
    world: World | None = None,
) -> Harness:
    """Write the overlay for ``profile``, load it as the node does and wire the fake ports."""
    fields = copy.deepcopy(OVERLAY if overlay is None else overlay)
    fields["transport"] = "in_memory" if profile == "inmemory" else "redpanda"
    install_node_overlay(monkeypatch, root, NODE, fields)
    config = load_agent_review_config()
    assert config.transport == fields["transport"], "the overlay selects the binding"
    world = world or World()
    handler = HandlerAgentReviewDispatch(
        config=config,
        head=FakeHead(world),
        lab=FakeLab(world),
        signer=FakeSigner(world),
        store=FakeStore(world),
        poster=FakePoster(world),
        terminal_log=FakeTerminalLog(world),
        clock=FakeClock(world),
    )
    return Harness(profile=profile, config=config, world=world, handler=handler)
