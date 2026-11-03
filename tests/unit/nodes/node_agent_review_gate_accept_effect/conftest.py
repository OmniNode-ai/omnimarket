# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every test here runs in both profiles: the in-memory overlay and the Redpanda overlay.

The ``profile`` fixture is autouse and parametrized, so each test function reports one
``[inmemory]`` and one ``[redpanda_overlay]`` result. Only the overlay's ``transport``
and the bus binding behind it differ; the request goes onto the contract's command topic,
the binding runs the one handler, and the decision is read off the decided topic. It must
equal the decision the handler returns directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_agent_review_gate_accept_effect import (
    NodeAgentReviewGateAcceptEffect,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.models import (
    EnumGateAcceptTransport,
    ModelGateAcceptRequest,
    ModelGateAcceptResult,
    ModelGateAcceptTopics,
    ModelReviewView,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.overlay import (
    load_gate_accept_policy,
    load_topics,
    load_transport,
)
from omnimarket.nodes.node_agent_review_gate_accept_effect.protocols import (
    ProtocolGateAcceptBus,
)
from tests.node_overlay_support import install_node_overlay

from .support import (
    HEAD,
    OUTPUT_PATH,
    PR,
    PUBLISHER,
    REPO,
    RESULT_PATH,
    SUMMARY_PATH,
    VERIFIER_ENDPOINT,
    BrokerDouble,
    FakeFiles,
    FakeReviews,
    FakeVerifier,
    InMemoryBus,
)


@dataclass(frozen=True)
class Profile:
    name: str
    transport: EnumGateAcceptTransport
    bus: ProtocolGateAcceptBus
    root: Path


@pytest.fixture(
    autouse=True,
    params=[
        pytest.param(EnumGateAcceptTransport.IN_MEMORY, id="inmemory"),
        pytest.param(EnumGateAcceptTransport.REDPANDA, id="redpanda_overlay"),
    ],
)
def profile(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Profile:
    transport: EnumGateAcceptTransport = request.param
    install_node_overlay(
        monkeypatch,
        tmp_path,
        "agent_review_gate_accept",
        {
            "publisher_login": PUBLISHER,
            "verifier_endpoint": VERIFIER_ENDPOINT,
            "transport": transport.value,
        },
    )
    assert load_transport() is transport, "the overlay selects the bus binding"
    bus: ProtocolGateAcceptBus = (
        InMemoryBus()
        if transport is EnumGateAcceptTransport.IN_MEMORY
        else BrokerDouble()
    )
    return Profile(
        "inmemory"
        if transport is EnumGateAcceptTransport.IN_MEMORY
        else "redpanda_overlay",
        transport,
        bus,
        tmp_path,
    )


def _unique(base: ModelGateAcceptTopics) -> ModelGateAcceptTopics:
    suffix = uuid4().hex[:12]
    return ModelGateAcceptTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        decided=f"{base.decided.removesuffix('.v1')}.{suffix}.v1",
    )


def request_for(**changes: object) -> ModelGateAcceptRequest:
    fields: dict[str, object] = {
        "repository": REPO,
        "pull_request": PR,
        "head_sha": HEAD,
        "voter_result_path": RESULT_PATH,
        "step_output_path": OUTPUT_PATH,
        "step_summary_path": SUMMARY_PATH,
    }
    fields.update(changes)
    return ModelGateAcceptRequest.model_validate(fields)


class Gate:
    """One node wired to fake ports, answering over the profile's bus."""

    def __init__(
        self,
        profile: Profile,
        files: FakeFiles,
        reviews: FakeReviews,
        verifier: FakeVerifier,
    ) -> None:
        self.profile = profile
        self.files = files
        self.reviews = reviews
        self.verifier = verifier
        self.node = NodeAgentReviewGateAcceptEffect(
            policy=load_gate_accept_policy(),
            reviews=reviews,
            verifier=verifier,
            files=files,
        )

    async def decide(self, request: ModelGateAcceptRequest) -> ModelGateAcceptResult:
        """The decision over the bus; the direct call must give the same one."""
        topics = _unique(load_topics())
        bus = self.profile.bus
        decided: asyncio.Queue[ModelGateAcceptResult] = asyncio.Queue()

        async def on_command(message: ModelEventMessage) -> None:
            result = self.node.handle(
                ModelGateAcceptRequest.model_validate_json(message.value)
            )
            await bus.publish(topics.decided, None, result.model_dump_json().encode())

        async def observe(message: ModelEventMessage) -> None:
            await decided.put(ModelGateAcceptResult.model_validate_json(message.value))

        stop_observing = await bus.subscribe_ungrouped(topics.decided, observe)
        stop_binding = await bus.subscribe_ungrouped(topics.command, on_command)
        try:
            await bus.publish(topics.command, None, request.model_dump_json().encode())
            return await asyncio.wait_for(decided.get(), timeout=30)
        finally:
            await stop_binding()
            await stop_observing()


@pytest.fixture
def make_gate(profile: Profile) -> Callable[..., Gate]:
    def build(
        result: str | None,
        reviews: list[ModelReviewView] | None = None,
        verifier: FakeVerifier | None = None,
        *,
        reviews_fail: bool = False,
    ) -> Gate:
        files = FakeFiles({} if result is None else {RESULT_PATH: result})
        return Gate(
            profile,
            files,
            FakeReviews(reviews or [], fail=reviews_fail),
            verifier or FakeVerifier(),
        )

    return build
