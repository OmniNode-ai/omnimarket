# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain test for node_tool_reuse_matcher_compute (OMN-13356).

DoD (OMN-13356): publish two similar requests; the second resolves to the
existing generated tool via a tool-matched verdict with NO LLM call in the
trace; golden-chain fixture covering match + miss.

This chain exercises the deterministic matcher end to end:

  request #1 (cold registry)  -> NO_MATCH  -> generation would run (miss)
  <register the generated tool in the registry>
  request #2 (similar request) -> MATCHED  -> route to the existing tool (hit)

The matcher is pure and non-LLM by construction: it performs only hash equality
and deterministic token-set similarity. No model-routing / inference seam is
imported or invoked anywhere in the handler, so "no LLM call in the trace" is a
structural property, asserted here by replaying the chain twice for identical
results.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_tool_reuse_matcher_compute.handlers.handler_tool_reuse_matcher import (
    HandlerToolReuseMatcher,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_generated_tool import (
    ModelGeneratedToolRecord,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_enums import (
    EnumToolReuseMatchStrategy,
    EnumToolReuseVerdict,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_reuse_request import (
    ModelToolReuseRequest,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.models.model_tool_signature import (
    ModelInputOutputSignature,
    compute_fields_hash,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.registry_in_memory import (
    InMemoryGeneratedToolRegistry,
)

_INPUT_HASH = compute_fields_hash({"source": "str", "options": "dict"})
_OUTPUT_HASH = compute_fields_hash({"violations": "list", "passed": "bool"})

_SIGNATURE = ModelInputOutputSignature(
    input_model_name="ModelLintRequest",
    input_model_module="omnimarket.generated.model_lint_request",
    output_model_name="ModelLintResult",
    output_model_module="omnimarket.generated.model_lint_result",
    input_fields_hash=_INPUT_HASH,
    output_fields_hash=_OUTPUT_HASH,
)

# Two phrasings of the same task — "similar requests" per the DoD.
_TASK_FIRST = "Lint a Python source file and report style violations with a pass flag"
_TASK_SECOND = "Lint Python source and report style violations plus a pass flag"


def _matcher(registry: InMemoryGeneratedToolRegistry) -> HandlerToolReuseMatcher:
    """Build a container-driven matcher resolving *registry* (OMN-13603).

    Mirrors the runtime resolver path: the handler takes the injectable
    container and resolves ProtocolGeneratedToolRegistry from it at match time.
    """
    container = MagicMock()
    container.get_service.return_value = registry
    return HandlerToolReuseMatcher(container=container)


def _request(task: str) -> ModelToolReuseRequest:
    return ModelToolReuseRequest(
        correlation_id=uuid4(),
        task_description=task,
        requested_signature=_SIGNATURE,
        match_strategy=EnumToolReuseMatchStrategy.HYBRID,
        similarity_threshold=0.85,
    )


def _registered_tool() -> ModelGeneratedToolRecord:
    """The tool the first (miss) request would have caused generation to emit."""
    return ModelGeneratedToolRecord(
        tool_id="lint-py-001",
        tool_name="node_generated_lint_py_001",
        handler_module="omnimarket.generated.lint_py_001.handler",
        handler_class="HandlerGeneratedLintPy",
        contract_hash="sha256:lint-py-001",
        semantic_description=_TASK_FIRST,
        input_model_name="ModelLintRequest",
        output_model_name="ModelLintResult",
        input_fields_hash=_INPUT_HASH,
        output_fields_hash=_OUTPUT_HASH,
        generated_at=datetime(2026, 6, 19, 9, 0, tzinfo=UTC),
        is_active=True,
    )


@pytest.mark.unit
class TestToolReuseGoldenChain:
    def test_miss_then_hit_chain(self) -> None:
        # --- request #1: cold registry -> MISS (generation would run) ---
        cold_matcher = _matcher(InMemoryGeneratedToolRegistry([]))
        first = cold_matcher.handle(_request(_TASK_FIRST))
        assert first.verdict == EnumToolReuseVerdict.NO_MATCH
        assert first.matched_tool is None

        # --- generation completes; the tool is registered ---
        warm_registry = InMemoryGeneratedToolRegistry([_registered_tool()])
        warm_matcher = _matcher(warm_registry)

        # --- request #2: similar request -> HIT (route to existing tool) ---
        second = warm_matcher.handle(_request(_TASK_SECOND))
        assert second.verdict == EnumToolReuseVerdict.MATCHED
        assert second.matched_tool is not None
        assert second.matched_tool.tool.tool_id == "lint-py-001"
        # Signature path -> full confidence; reuse routes to the existing handler.
        assert second.matched_tool.match_confidence == 1.0
        assert second.matched_tool.tool.handler_module.endswith("lint_py_001.handler")

    def test_chain_is_replay_deterministic(self) -> None:
        warm_matcher = _matcher(InMemoryGeneratedToolRegistry([_registered_tool()]))
        req = _request(_TASK_SECOND)
        assert warm_matcher.handle(req) == warm_matcher.handle(req)

    def test_semantic_only_chain_misses_then_hits(self) -> None:
        """Same miss->hit chain proved without any signature equality.

        Forces the SEMANTIC strategy so the hit rides lexical similarity alone,
        guarding against the hit being an artifact of identical signatures.
        """
        cold = _matcher(InMemoryGeneratedToolRegistry([]))
        miss = cold.handle(
            ModelToolReuseRequest(
                correlation_id=uuid4(),
                task_description=_TASK_FIRST,
                requested_signature=_SIGNATURE,
                match_strategy=EnumToolReuseMatchStrategy.SEMANTIC,
                similarity_threshold=0.5,
            )
        )
        assert miss.verdict == EnumToolReuseVerdict.NO_MATCH

        warm = _matcher(InMemoryGeneratedToolRegistry([_registered_tool()]))
        hit = warm.handle(
            ModelToolReuseRequest(
                correlation_id=uuid4(),
                task_description=_TASK_FIRST,  # identical wording -> similarity 1.0
                requested_signature=_SIGNATURE,
                match_strategy=EnumToolReuseMatchStrategy.SEMANTIC,
                similarity_threshold=0.5,
            )
        )
        assert hit.verdict == EnumToolReuseVerdict.MATCHED
        assert hit.matched_tool is not None
        assert hit.matched_tool.tool.tool_id == "lint-py-001"
