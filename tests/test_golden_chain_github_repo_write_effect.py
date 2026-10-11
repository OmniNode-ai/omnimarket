# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_github_repo_write_effect over its real contract (OMN-20912).

Topic bytes in, typed result out, through the runtime's own path: discovery
reads ``contract.yaml``, ``build_routing_map`` resolves the request topic to the
handler entry and its ``event_model``, ``RuntimeDispatch`` decodes the envelope
from an ``InMemoryTransport`` and coerces the payload into
``ModelRepoWriteRequest``, the handler runs over a strict recorded fake
transport, and the result goes out on the topic the contract names for its
class. Three legs: a pr_comment that posts (completed), a dry_run that sends
nothing (completed), and a pr_close with no claim (failed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_core.runtime.transport.runtime_in_memory_broker import InMemoryBroker
from omnibase_core.runtime.transport.runtime_in_memory_transport import (
    InMemoryTransport,
)
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic
from omnibase_infra.runtime.core_runtime.routing_map_builder import build_routing_map

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    create_issue_comment_request,
    issue_comments_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers import (
    HandlerGithubRepoWriteEffect,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_github_repo_write import (
    idempotency_marker,
)
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_github_repo_write_effect/contract.yaml"
_REQUESTED = "onex.cmd.omnimarket.github-repo-write-requested.v1"
_COMPLETED = "onex.evt.omnimarket.github-repo-write-completed.v1"
_FAILED = "onex.evt.omnimarket.github-repo-write-failed.v1"
_GROUP = "onex.core-runtime.github-repo-write"
_CLOCK = datetime(2026, 10, 10, 19, 0, 0, tzinfo=UTC)
_REPO = "OmniNode-ai/omnimarket"
_HEAD = "4ceedeafeb4040c987883d96c20e745ad5eed6e1"
_KEY = b"OmniNode-ai/omnimarket#4000"
_HEADERS = {
    "x-ratelimit-limit": "5000",
    "x-ratelimit-remaining": "4900",
    "x-ratelimit-used": "100",
    "x-ratelimit-reset": str(int(_CLOCK.timestamp()) + 3600),
    "x-ratelimit-resource": "core",
}


@dataclass
class _Recorded:
    exchanges: list[tuple[ModelGithubHttpRequest, ModelGithubHttpResponse]]
    sent: list[ModelGithubHttpRequest] = field(default_factory=list)

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        self.sent.append(request)
        expected, response = self.exchanges.pop(0)
        assert request == expected, f"sent {request!r}, recorded {expected!r}"
        return response


def _chain(
    handler: HandlerGithubRepoWriteEffect,
) -> tuple[RuntimeDispatch, InMemoryTransport, InMemoryBroker]:
    manifest = discover_contracts_from_paths([_CONTRACT])
    assert not manifest.errors, manifest.errors
    (contract,) = manifest.contracts
    assert contract.event_bus is not None
    topics = frozenset(contract.event_bus.subscribe_topics)
    assert topics == {_REQUESTED}
    routing_map = build_routing_map(
        [contract], topics, handler_resolver=lambda _ref: handler
    )
    assert set(routing_map) == topics
    broker = InMemoryBroker(num_partitions=1)
    transport = InMemoryTransport(broker=broker, group=_GROUP, topics=sorted(topics))
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: _CLOCK,
    )
    return dispatch, transport, broker


def _on(broker: InMemoryBroker, topic: str) -> list[ModelEventEnvelope[object]]:
    return [
        ModelEventEnvelope[object].model_validate_json(record.value)
        for record in broker.records(topic, 0)
    ]


async def _run(
    handler: HandlerGithubRepoWriteEffect, command: ModelRepoWriteRequest
) -> tuple[InMemoryBroker, UUID]:
    dispatch, transport, broker = _chain(handler)
    correlation_id = uuid4()
    envelope = ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=command.model_dump(mode="json"),
        correlation_id=correlation_id,
        event_type="golden-chain.inbound",
        payload_type="ModelRepoWriteRequest",
    )
    await transport.send(
        _REQUESTED,
        key=_KEY,
        value=envelope.model_dump_json().encode("utf-8"),
        headers={},
    )
    assert await dispatch.drain() == 1
    return broker, correlation_id


def _command(op: EnumRepoWriteOperation, **kw: object) -> ModelRepoWriteRequest:
    return ModelRepoWriteRequest.model_validate(
        {
            "correlation_id": uuid4(),
            "operation": op,
            "repo": _REPO,
            "idempotency_key": "golden-chain-omn20912",
            "requested_by_lane": "scm-ops-5d21",
            **kw,
        }
    )


async def test_a_posted_comment_is_published_as_completed() -> None:
    marker = idempotency_marker("golden-chain-omn20912")
    fake = _Recorded(
        [
            (
                issue_comments_request(_REPO, 4000, per_page=LIST_PAGE_SIZE),
                ModelGithubHttpResponse(
                    status=200, headers=_HEADERS, body={"value": []}
                ),
            ),
            (
                create_issue_comment_request(_REPO, 4000, f"lab note\n\n{marker}"),
                ModelGithubHttpResponse(
                    status=201, headers=_HEADERS, body={"id": 91, "html_url": "u"}
                ),
            ),
        ]
    )
    handler = HandlerGithubRepoWriteEffect(fake, clock=_CLOCK.timestamp)
    broker, correlation_id = await _run(
        handler,
        _command(EnumRepoWriteOperation.PR_COMMENT, pr_number=4000, body="lab note"),
    )
    (envelope,) = _on(broker, _COMPLETED)
    assert envelope.correlation_id == correlation_id
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.mode is EnumRepoWriteMode.ENFORCE
    assert completed.comment_id == 91
    assert _on(broker, _FAILED) == []


async def test_a_dry_run_sends_nothing_and_completes() -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(fake, clock=_CLOCK.timestamp)
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.WORKFLOW_DISPATCH,
            workflow="ci.yml",
            ref="dev",
            dry_run=True,
        ),
    )
    assert fake.sent == []
    (envelope,) = _on(broker, _COMPLETED)
    completed = ModelRepoWriteCompleted.model_validate(envelope.payload)
    assert completed.mode is EnumRepoWriteMode.DRY_RUN
    assert completed.requests


async def test_a_close_without_a_claim_is_published_as_failed(tmp_path: Path) -> None:
    fake = _Recorded([])
    handler = HandlerGithubRepoWriteEffect(
        fake, claim_lookup=HandlerPrClaimLookup(), clock=_CLOCK.timestamp
    )
    broker, _ = await _run(
        handler,
        _command(
            EnumRepoWriteOperation.PR_CLOSE,
            pr_number=4000,
            expected_head_sha=_HEAD,
            claims_dir=str(tmp_path),
            requested_by_run="run-golden",
        ),
    )
    assert fake.sent == []
    assert _on(broker, _COMPLETED) == []
    (envelope,) = _on(broker, _FAILED)
    failed = ModelRepoWriteFailed.model_validate(envelope.payload)
    assert failed.reason is EnumRepoWriteRefusal.CLAIM_NOT_HELD
    assert failed.terminal_failure_cause is EnumRepoWriteRefusal.CLAIM_NOT_HELD
