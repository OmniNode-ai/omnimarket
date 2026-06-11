# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerArtifactResolver (OMN-12948).

Verifies the resolver materialises a real per-factor content map from pre-read
artifact sources, reusing the GuidanceSectionParser for section selection and
the pack-builder's budget/precedence authority. Pure COMPUTE: no I/O.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.nodes.node_context_artifact_resolver_compute.handlers.handler_artifact_resolver import (
    HandlerArtifactResolver,
    estimate_tokens,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_request import (
    ModelArtifactResolverRequest,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_result import (
    EnumArtifactResolverStatus,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_source import (
    ModelArtifactSource,
)

_GENERATED_AT = "2026-06-11T00:00:00+00:00"
_CONTRACT_HASH = "a" * 64


def _source(
    factor: EnumContextFactor,
    raw_content: str,
    *,
    sectioned: bool = False,
    provenance: EnumContextPackProvenance = EnumContextPackProvenance.CURATED,
    source_name: str | None = None,
) -> ModelArtifactSource:
    return ModelArtifactSource(
        factor=factor,
        source_name=source_name or f"{factor.value}.src",
        raw_content=raw_content,
        provenance=provenance,
        source_contract_hash=_CONTRACT_HASH,
        is_markdown_sectioned=sectioned,
    )


def _request(
    *sources: ModelArtifactSource,
    token_budget: int = 16000,
    per_factor_token_budget: int = 3000,
) -> ModelArtifactResolverRequest:
    return ModelArtifactResolverRequest(
        contract_hash=_CONTRACT_HASH,
        generated_at=_GENERATED_AT,
        sources=tuple(sources),
        token_budget=token_budget,
        per_factor_token_budget=per_factor_token_budget,
    )


# ---------------------------------------------------------------------------
# token estimate heuristic
# ---------------------------------------------------------------------------


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_non_empty_at_least_one() -> None:
    assert estimate_tokens("a") == 1
    # 8 chars -> ceil(8/4) == 2
    assert estimate_tokens("abcdefgh") == 2
    # 9 chars -> ceil(9/4) == 3
    assert estimate_tokens("abcdefghi") == 3


# ---------------------------------------------------------------------------
# whole-body (non-sectioned) factors
# ---------------------------------------------------------------------------


def test_whole_body_factor_resolves_real_content() -> None:
    handler = HandlerArtifactResolver()
    result = handler.handle(
        _request(
            _source(EnumContextFactor.GOLDEN_CHAIN, "chains:\n  - name: registration"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert "golden_chain" in result.artifact_content_map
    assert (
        result.artifact_content_map["golden_chain"] == "chains:\n  - name: registration"
    )
    assert result.resolved_factors == ("golden_chain",)
    assert result.pack_hash is not None


def test_no_stub_placeholder_text_in_output() -> None:
    handler = HandlerArtifactResolver()
    result = handler.handle(
        _request(
            _source(EnumContextFactor.GOLDEN_CHAIN, "real golden chain text"),
            _source(EnumContextFactor.EXEMPLAR, "real exemplar code"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    for content in result.artifact_content_map.values():
        assert "stub content for" not in content


# ---------------------------------------------------------------------------
# markdown section selection
# ---------------------------------------------------------------------------


def test_sectioned_markdown_splits_and_selects_within_budget() -> None:
    # Each section ~ 200 chars => ~50 tokens. per_factor budget 120 tokens fits
    # ~2 sections, the third overflows and is dropped.
    body = "\n".join(f"# Section {i}\n" + ("x" * 196) for i in range(1, 5))
    handler = HandlerArtifactResolver()
    result = handler.handle(
        _request(
            _source(
                EnumContextFactor.CLAUDE_MD,
                body,
                sectioned=True,
                source_name="CLAUDE.md",
            ),
            per_factor_token_budget=120,
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    content = result.artifact_content_map["claude_md"]
    # Leading sections selected, later ones dropped.
    assert "Section 1" in content
    assert "Section 4" not in content
    # A selection warning is emitted when sections are dropped.
    assert any("selected" in w for w in result.warnings)


def test_sectioned_markdown_without_headings_uses_whole_body() -> None:
    handler = HandlerArtifactResolver()
    result = handler.handle(
        _request(
            _source(
                EnumContextFactor.ARCHITECTURE_PATTERNS,
                "no headings here, just prose about nodes and handlers",
                sectioned=True,
            ),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert "no headings here" in result.artifact_content_map["architecture_patterns"]
    assert any("no headings" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# precedence ordering
# ---------------------------------------------------------------------------


def test_resolved_factors_in_canonical_precedence_order() -> None:
    handler = HandlerArtifactResolver()
    # Supply sources out of precedence order; expect canonical order back.
    result = handler.handle(
        _request(
            _source(EnumContextFactor.CLAUDE_MD, "# C\nclaude md", sectioned=True),
            _source(EnumContextFactor.GOLDEN_CHAIN, "golden"),
            _source(EnumContextFactor.EXEMPLAR, "exemplar"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    # golden_chain < exemplar < claude_md
    assert result.resolved_factors == ("golden_chain", "exemplar", "claude_md")


# ---------------------------------------------------------------------------
# budget hard-reject (negative control path)
# ---------------------------------------------------------------------------


def test_token_budget_exceeded_fails_closed() -> None:
    # Whole-body source way over the overall budget; the pack-builder
    # TOKEN_BUDGET_EXCEEDED hard-reject must fire (never silent truncation).
    huge = "x" * 80000  # ~20000 tokens, over a 16000 budget
    handler = HandlerArtifactResolver()
    result = handler.handle(
        _request(
            _source(EnumContextFactor.CLAUDE_MD, huge),
            token_budget=16000,
        )
    )
    assert result.status is EnumArtifactResolverStatus.FAILED
    assert result.failure_class == "token_budget_exceeded"
    assert result.artifact_content_map == {}


# ---------------------------------------------------------------------------
# determinism + empty input
# ---------------------------------------------------------------------------


def test_resolution_is_deterministic() -> None:
    handler = HandlerArtifactResolver()
    req = _request(
        _source(EnumContextFactor.GOLDEN_CHAIN, "golden"),
        _source(EnumContextFactor.EXEMPLAR, "exemplar body"),
    )
    first = handler.handle(req)
    second = handler.handle(req)
    assert first.artifact_content_map == second.artifact_content_map
    assert first.pack_hash == second.pack_hash


def test_empty_sources_fails() -> None:
    handler = HandlerArtifactResolver()
    result = handler.handle(_request())
    assert result.status is EnumArtifactResolverStatus.FAILED
    assert result.errors


@pytest.mark.parametrize(
    "factor",
    [
        EnumContextFactor.GOLDEN_CHAIN,
        EnumContextFactor.EXEMPLAR,
        EnumContextFactor.LOCAL_FAILURES,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
        EnumContextFactor.CLAUDE_MD,
    ],
)
def test_every_canonical_factor_resolves(factor: EnumContextFactor) -> None:
    handler = HandlerArtifactResolver()
    sectioned = factor in (
        EnumContextFactor.CLAUDE_MD,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
    )
    body = (
        f"# Head\nreal content for {factor.value}"
        if sectioned
        else f"real {factor.value}"
    )
    result = handler.handle(_request(_source(factor, body, sectioned=sectioned)))
    assert result.status is EnumArtifactResolverStatus.OK
    assert factor.value in result.artifact_content_map
    assert result.artifact_content_map[factor.value].strip() != ""
