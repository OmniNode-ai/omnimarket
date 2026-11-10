# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fakes for node_github_git_credential_effect tests (OMN-22613)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.handlers.scm.scm_git_credential import node_topics
from omnimarket.nodes.node_github_git_credential_effect.handlers import (
    HandlerGithubGitCredentialEffect,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    ModelGitCredentialRequest,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    MintedRepositoryToken,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = ROOT / "src/omnimarket/nodes/node_github_git_credential_effect/contract.yaml"
# Derived from the contract, never spelled here.
_TOPICS = node_topics()
REQUESTED = _TOPICS.command
ISSUED = _TOPICS.success
FAILED = _TOPICS.failure or ""
GROUP = "onex.core-runtime.github-git-credential"
CLOCK = datetime(2026, 11, 6, 10, 0, 0, tzinfo=UTC)
# A fake, shaped like an installation token so a leak check has something to find.
FAKE_TOKEN = "ghs_FAKEgoldenchainTOKEN0000000000000000"
PERMISSIONS = {"contents": "write", "workflows": "write", "metadata": "read"}


@dataclass
class FakeMinter:
    """Records every mint and answers with a token narrowed as asked."""

    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    answer: Callable[[str, str, Mapping[str, str]], MintedRepositoryToken] | None = None
    error: Exception | None = None

    async def mint(
        self, *, owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken:
        self.calls.append((owner, name, dict(permissions)))
        if self.error is not None:
            raise self.error
        if self.answer is not None:
            return self.answer(owner, name, permissions)
        return MintedRepositoryToken(
            token=FAKE_TOKEN,
            expires_at="2026-11-06T11:00:00Z",
            permissions=dict(permissions),
            repositories=(name,),
        )


def chain(
    handler: HandlerGithubGitCredentialEffect,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    """The runtime's own path: discovery, routing map, dispatch over a transport."""
    manifest = discover_contracts_from_paths([CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    assert topics == {REQUESTED}
    routing_map = build_routing_map(
        [contract], topics, handler_resolver=lambda _ref: handler
    )
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: CLOCK,
    )
    return dispatch, transport, broker


def on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


def raw_records(broker: InMemoryBroker, *topics: str) -> bytes:
    return b"".join(record.value for t in topics for record in broker.records(t, 0))


async def run(
    handler: HandlerGithubGitCredentialEffect, request: ModelGitCredentialRequest
) -> tuple[InMemoryBroker, UUID]:
    dispatch, transport, broker = chain(handler)
    correlation_id = uuid4()
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=request.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="golden-chain.inbound",
        payload_type="ModelGitCredentialRequest",
    )
    await transport.send(
        REQUESTED,
        key=request.repository.encode("utf-8"),
        value=envelope.model_dump_json().encode("utf-8"),
        headers={},
    )
    assert await dispatch.drain() == 1
    return broker, correlation_id
