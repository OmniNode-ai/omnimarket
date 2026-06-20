# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerToolReuseMatcher (OMN-13356).

Pure deterministic matcher: signature fast path, lexical-similarity fallback,
verdict selection, registry-failure handling, and replay determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from omnimarket.nodes.node_tool_reuse_matcher_compute.protocols.protocol_generated_tool_registry import (
    ProtocolGeneratedToolRegistry,
)
from omnimarket.nodes.node_tool_reuse_matcher_compute.registry_in_memory import (
    InMemoryGeneratedToolRegistry,
)

_INPUT_HASH = compute_fields_hash({"source": "str", "limit": "int"})
_OUTPUT_HASH = compute_fields_hash({"findings": "list", "count": "int"})
_OTHER_INPUT_HASH = compute_fields_hash({"text": "str"})
_OTHER_OUTPUT_HASH = compute_fields_hash({"summary": "str"})


def _signature(
    *,
    input_hash: str = _INPUT_HASH,
    output_hash: str = _OUTPUT_HASH,
) -> ModelInputOutputSignature:
    return ModelInputOutputSignature(
        input_model_name="ModelScanRequest",
        input_model_module="omnimarket.generated.model_scan_request",
        output_model_name="ModelScanResult",
        output_model_module="omnimarket.generated.model_scan_result",
        input_fields_hash=input_hash,
        output_fields_hash=output_hash,
    )


def _tool(
    *,
    tool_id: str,
    description: str = "Scan source text and return findings with a count",
    input_hash: str = _INPUT_HASH,
    output_hash: str = _OUTPUT_HASH,
    generated_at: datetime | None = None,
    is_active: bool = True,
) -> ModelGeneratedToolRecord:
    return ModelGeneratedToolRecord(
        tool_id=tool_id,
        tool_name=f"node_generated_{tool_id}",
        handler_module=f"omnimarket.generated.{tool_id}.handler",
        handler_class="HandlerGenerated",
        contract_hash=f"sha256:{tool_id}",
        semantic_description=description,
        input_model_name="ModelScanRequest",
        output_model_name="ModelScanResult",
        input_fields_hash=input_hash,
        output_fields_hash=output_hash,
        generated_at=generated_at or datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
        is_active=is_active,
    )


def _request(
    *,
    task: str = "Scan source text and return findings with a count",
    strategy: EnumToolReuseMatchStrategy = EnumToolReuseMatchStrategy.HYBRID,
    threshold: float = 0.85,
    signature: ModelInputOutputSignature | None = None,
) -> ModelToolReuseRequest:
    return ModelToolReuseRequest(
        correlation_id=uuid4(),
        task_description=task,
        requested_signature=signature or _signature(),
        match_strategy=strategy,
        similarity_threshold=threshold,
    )


class _RaisingRegistry:
    """Registry stub that fails every query — exercises the failure path."""

    def query_by_signature(
        self, *, input_fields_hash: str, output_fields_hash: str
    ) -> list[ModelGeneratedToolRecord]:
        raise RuntimeError("registry connection refused")

    def list_active(self) -> list[ModelGeneratedToolRecord]:
        raise RuntimeError("registry connection refused")


def _matcher(*tools: ModelGeneratedToolRecord) -> HandlerToolReuseMatcher:
    registry: ProtocolGeneratedToolRegistry = InMemoryGeneratedToolRegistry(tools)
    return HandlerToolReuseMatcher(registry)


@pytest.mark.unit
class TestSignatureStrategy:
    def test_exact_signature_match_returns_matched(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha"))
        result = matcher.handle(_request(strategy=EnumToolReuseMatchStrategy.SIGNATURE))
        assert result.verdict == EnumToolReuseVerdict.MATCHED
        assert result.matched_tool is not None
        assert result.matched_tool.tool.tool_id == "alpha"
        assert result.matched_tool.match_confidence == 1.0

    def test_no_signature_match_returns_no_match(self) -> None:
        matcher = _matcher(
            _tool(
                tool_id="alpha",
                input_hash=_OTHER_INPUT_HASH,
                output_hash=_OTHER_OUTPUT_HASH,
            )
        )
        result = matcher.handle(_request(strategy=EnumToolReuseMatchStrategy.SIGNATURE))
        assert result.verdict == EnumToolReuseVerdict.NO_MATCH
        assert result.matched_tool is None

    def test_multiple_signature_matches_is_ambiguous(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha"), _tool(tool_id="beta"))
        result = matcher.handle(_request(strategy=EnumToolReuseMatchStrategy.SIGNATURE))
        assert result.verdict == EnumToolReuseVerdict.AMBIGUOUS
        assert result.matched_tool is None
        assert len(result.candidate_tools) == 2

    def test_inactive_tool_is_not_matched(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha", is_active=False))
        result = matcher.handle(_request(strategy=EnumToolReuseMatchStrategy.SIGNATURE))
        assert result.verdict == EnumToolReuseVerdict.NO_MATCH


@pytest.mark.unit
class TestSemanticStrategy:
    def test_similar_description_above_threshold_is_matched(self) -> None:
        matcher = _matcher(
            _tool(
                tool_id="alpha",
                description="Scan source text and return findings with a count",
            )
        )
        # Identical wording -> similarity 1.0.
        result = matcher.handle(
            _request(
                strategy=EnumToolReuseMatchStrategy.SEMANTIC,
                task="Scan source text and return findings with a count",
                threshold=0.85,
            )
        )
        assert result.verdict == EnumToolReuseVerdict.MATCHED
        assert result.matched_tool is not None
        assert result.matched_tool.tool.tool_id == "alpha"

    def test_dissimilar_description_below_threshold_is_no_match(self) -> None:
        matcher = _matcher(
            _tool(tool_id="alpha", description="Render a markdown table from CSV rows")
        )
        result = matcher.handle(
            _request(
                strategy=EnumToolReuseMatchStrategy.SEMANTIC,
                task="Compute the SHA-256 digest of a binary blob and return hex",
                threshold=0.85,
            )
        )
        assert result.verdict == EnumToolReuseVerdict.NO_MATCH

    def test_semantic_confidence_is_in_unit_interval(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha"))
        result = matcher.handle(
            _request(strategy=EnumToolReuseMatchStrategy.SEMANTIC, threshold=0.0)
        )
        assert result.candidate_tools
        for cand in result.candidate_tools:
            assert 0.0 <= cand.match_confidence <= 1.0


@pytest.mark.unit
class TestHybridStrategy:
    def test_hybrid_prefers_signature_over_semantic(self) -> None:
        # signature_tool matches the requested signature exactly; semantic_tool
        # has a closer description but a different signature.
        signature_tool = _tool(
            tool_id="sig",
            description="totally unrelated wording about widgets",
        )
        semantic_tool = _tool(
            tool_id="sem",
            description="Scan source text and return findings with a count",
            input_hash=_OTHER_INPUT_HASH,
            output_hash=_OTHER_OUTPUT_HASH,
        )
        matcher = _matcher(signature_tool, semantic_tool)
        result = matcher.handle(_request(strategy=EnumToolReuseMatchStrategy.HYBRID))
        assert result.verdict == EnumToolReuseVerdict.MATCHED
        assert result.matched_tool is not None
        assert result.matched_tool.tool.tool_id == "sig"
        assert result.matched_tool.match_confidence == 1.0

    def test_hybrid_falls_back_to_semantic_when_no_signature(self) -> None:
        semantic_tool = _tool(
            tool_id="sem",
            description="Scan source text and return findings with a count",
            input_hash=_OTHER_INPUT_HASH,
            output_hash=_OTHER_OUTPUT_HASH,
        )
        matcher = _matcher(semantic_tool)
        result = matcher.handle(
            _request(
                strategy=EnumToolReuseMatchStrategy.HYBRID,
                task="Scan source text and return findings with a count",
                threshold=0.5,
            )
        )
        assert result.verdict == EnumToolReuseVerdict.MATCHED
        assert result.matched_tool is not None
        assert result.matched_tool.tool.tool_id == "sem"


@pytest.mark.unit
class TestFailureAndContract:
    def test_registry_failure_returns_registry_unavailable(self) -> None:
        matcher = HandlerToolReuseMatcher(_RaisingRegistry())
        result = matcher.handle(_request())
        assert result.verdict == EnumToolReuseVerdict.REGISTRY_UNAVAILABLE
        assert result.failure_reason is not None
        assert "registry connection refused" in result.failure_reason

    def test_handle_accepts_mapping_payload(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha"))
        payload = _request(strategy=EnumToolReuseMatchStrategy.SIGNATURE).model_dump(
            mode="json"
        )
        result = matcher.handle(payload)
        assert result.verdict == EnumToolReuseVerdict.MATCHED

    def test_correlation_id_is_echoed(self) -> None:
        req = _request(strategy=EnumToolReuseMatchStrategy.SIGNATURE)
        matcher = _matcher(_tool(tool_id="alpha"))
        result = matcher.handle(req)
        assert result.correlation_id == req.correlation_id

    def test_max_candidates_caps_returned_list(self) -> None:
        tools = [
            _tool(tool_id=f"t{i}", input_hash=_OTHER_INPUT_HASH) for i in range(10)
        ]
        matcher = _matcher(*tools)
        req = ModelToolReuseRequest(
            correlation_id=uuid4(),
            task_description="Scan source text and return findings with a count",
            requested_signature=_signature(),
            match_strategy=EnumToolReuseMatchStrategy.SEMANTIC,
            similarity_threshold=0.0,
            max_candidates=3,
        )
        result = matcher.handle(req)
        assert len(result.candidate_tools) == 3

    def test_same_input_is_replay_deterministic(self) -> None:
        matcher = _matcher(_tool(tool_id="alpha"))
        req = _request(strategy=EnumToolReuseMatchStrategy.SIGNATURE)
        first = matcher.handle(req)
        second = matcher.handle(req)
        assert first == second
