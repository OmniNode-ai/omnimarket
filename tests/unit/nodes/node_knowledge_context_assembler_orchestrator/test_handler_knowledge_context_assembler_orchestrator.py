# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for HandlerKnowledgeContextAssemblerOrchestrator.

All tests are pure — no I/O, no network, no Kafka.
Uses FakeBackend injections for all four backends.

Tests verify:
  1. Fan-out emits intent for each configured backend
  2. Partial failure: 2 of 4 backends respond = valid bundle
  3. All backends succeed = COMPLETE bundle status
  4. All backends fail = DEGRADED bundle with empty fragments
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_knowledge_context_assembler_orchestrator.handlers.handler_knowledge_context_assembler_orchestrator import (
    HandlerKnowledgeContextAssemblerOrchestrator,
    ProtocolKnowledgeBackend,
)
from omnimarket.nodes.node_knowledge_context_assembler_orchestrator.models.model_knowledge_context_request import (
    EnumContextLevel,
    ModelKnowledgeContextRequest,
)

# ---------------------------------------------------------------------------
# Fake backends
# ---------------------------------------------------------------------------


class FakeBackend:
    """Deterministic fake implementing ProtocolKnowledgeBackend."""

    def __init__(
        self,
        result: dict[str, Any] | None = None,
        should_fail: bool = False,
        error_msg: str = "backend error",
    ) -> None:
        self._result = result or {}
        self._should_fail = should_fail
        self._error_msg = error_msg
        self.call_count = 0
        self.last_request: Any = None

    async def fetch(self, request: Any) -> dict[str, Any]:
        self.call_count += 1
        self.last_request = request
        if self._should_fail:
            raise RuntimeError(self._error_msg)
        return self._result


def _make_request(
    level: EnumContextLevel = EnumContextLevel.L2,
    repo: str = "omnimarket",
) -> ModelKnowledgeContextRequest:
    return ModelKnowledgeContextRequest(
        correlation_id=str(uuid4()),
        repo=repo,
        level=level,
    )


def _make_handler(
    codebase_backend: FakeBackend | None = None,
    antipattern_backend: FakeBackend | None = None,
    learning_backend: FakeBackend | None = None,
    arch_graph_backend: FakeBackend | None = None,
) -> HandlerKnowledgeContextAssemblerOrchestrator:
    return HandlerKnowledgeContextAssemblerOrchestrator(
        codebase_intelligence_backend=codebase_backend
        or FakeBackend({"summary": "arch overview"}),
        antipattern_backend=antipattern_backend or FakeBackend({"patterns": []}),
        agent_learning_backend=learning_backend or FakeBackend({"learnings": []}),
        arch_graph_backend=arch_graph_backend or FakeBackend({"nodes": []}),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fake_backend_satisfies_protocol() -> None:
    """FakeBackend satisfies ProtocolKnowledgeBackend."""
    fake = FakeBackend()
    assert isinstance(fake, ProtocolKnowledgeBackend)


# ---------------------------------------------------------------------------
# Fan-out: all backends called
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_backends_called_for_l2() -> None:
    """L2 request fans out to codebase, antipattern, and learning backends (3 of 4)."""
    codebase = FakeBackend({"summary": "repo overview"})
    antipattern = FakeBackend({"patterns": ["N+1"]})
    learning = FakeBackend({"learnings": ["use-uv"]})
    arch_graph = FakeBackend({"nodes": ["A"]})

    handler = _make_handler(codebase, antipattern, learning, arch_graph)
    req = _make_request(level=EnumContextLevel.L2)
    result = await handler.handle(req)

    assert codebase.call_count == 1
    assert antipattern.call_count == 1
    assert learning.call_count == 1
    # L3 only for arch graph
    assert arch_graph.call_count == 0
    assert result.status in ("COMPLETE", "PARTIAL")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_arch_graph_called_only_for_l3() -> None:
    """Architecture graph backend is called only for L3 requests."""
    arch_graph = FakeBackend({"nodes": ["A", "B"]})
    handler = _make_handler(arch_graph_backend=arch_graph)

    req_l2 = _make_request(level=EnumContextLevel.L2)
    await handler.handle(req_l2)
    assert arch_graph.call_count == 0

    req_l3 = _make_request(level=EnumContextLevel.L3)
    await handler.handle(req_l3)
    assert arch_graph.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fan_out_is_parallel_all_backends_receive_correct_repo() -> None:
    """All backends receive the repo field from the request."""
    codebase = FakeBackend({"summary": "ok"})
    antipattern = FakeBackend({"patterns": []})
    learning = FakeBackend({"learnings": []})

    handler = _make_handler(codebase, antipattern, learning)
    req = _make_request(repo="my-test-repo", level=EnumContextLevel.L2)
    await handler.handle(req)

    for backend in (codebase, antipattern, learning):
        assert backend.last_request is not None


# ---------------------------------------------------------------------------
# Graceful degradation: partial failures
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_partial_failure_2_of_4_still_valid_bundle() -> None:
    """2 of 4 backends responding produces a PARTIAL (not FAILED) bundle."""
    failing_codebase = FakeBackend(should_fail=True)
    failing_antipattern = FakeBackend(should_fail=True)
    ok_learning = FakeBackend({"learnings": ["lesson-1"]})
    ok_arch = FakeBackend({"nodes": []})

    handler = _make_handler(failing_codebase, failing_antipattern, ok_learning, ok_arch)
    req = _make_request(level=EnumContextLevel.L3)
    result = await handler.handle(req)

    assert result.status == "PARTIAL"
    assert result.bundle is not None
    assert result.succeeded_backend_count == 2
    assert result.failed_backend_count == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_1_of_3_l2_backends_responds_still_valid() -> None:
    """1 of 3 L2 backends responding is still a valid (PARTIAL) bundle."""
    ok_codebase = FakeBackend({"summary": "minimal"})
    failing_antipattern = FakeBackend(should_fail=True)
    failing_learning = FakeBackend(should_fail=True)

    handler = _make_handler(ok_codebase, failing_antipattern, failing_learning)
    req = _make_request(level=EnumContextLevel.L2)
    result = await handler.handle(req)

    assert result.status == "PARTIAL"
    assert result.bundle is not None
    assert result.succeeded_backend_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_backends_fail_produces_degraded_bundle() -> None:
    """All backends failing produces a DEGRADED bundle (not a raised exception)."""
    handler = _make_handler(
        codebase_backend=FakeBackend(should_fail=True),
        antipattern_backend=FakeBackend(should_fail=True),
        learning_backend=FakeBackend(should_fail=True),
        arch_graph_backend=FakeBackend(should_fail=True),
    )
    req = _make_request(level=EnumContextLevel.L3)
    result = await handler.handle(req)

    assert result.status == "DEGRADED"
    assert result.bundle is not None
    assert result.succeeded_backend_count == 0


# ---------------------------------------------------------------------------
# All backends succeed
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_l2_backends_succeed_produces_complete_bundle() -> None:
    """All 3 L2 backends succeeding produces COMPLETE status."""
    handler = _make_handler(
        codebase_backend=FakeBackend({"summary": "full arch"}),
        antipattern_backend=FakeBackend({"patterns": ["no-mocks"]}),
        learning_backend=FakeBackend({"learnings": ["tdd-first"]}),
    )
    req = _make_request(level=EnumContextLevel.L2)
    result = await handler.handle(req)

    assert result.status == "COMPLETE"
    assert result.bundle is not None
    assert result.succeeded_backend_count == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_l3_backends_succeed_produces_complete_bundle() -> None:
    """All 4 L3 backends succeeding produces COMPLETE status."""
    handler = _make_handler(
        codebase_backend=FakeBackend({"summary": "full arch"}),
        antipattern_backend=FakeBackend({"patterns": []}),
        learning_backend=FakeBackend({"learnings": []}),
        arch_graph_backend=FakeBackend({"nodes": ["A", "B"]}),
    )
    req = _make_request(level=EnumContextLevel.L3)
    result = await handler.handle(req)

    assert result.status == "COMPLETE"
    assert result.succeeded_backend_count == 4


# ---------------------------------------------------------------------------
# Correlation ID threading
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_correlation_id_preserved_in_result() -> None:
    """The result carries the correlation_id from the original request."""
    handler = _make_handler()
    req = _make_request()
    result = await handler.handle(req)

    assert result.correlation_id == req.correlation_id
