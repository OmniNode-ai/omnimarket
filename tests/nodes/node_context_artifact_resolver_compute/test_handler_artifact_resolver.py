# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerArtifactResolver (OMN-12948, OMN-21137).

Verifies the resolver turns pre-read artifact sources into pack-builder
artifacts with provenance and applies none of the builder's policy: no section
selection, no dedup, no token budget and no pack hash. Pure COMPUTE: no I/O.
"""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from pydantic import ValidationError

from omnimarket.nodes.node_context_artifact_resolver_compute.handlers.handler_artifact_resolver import (
    HandlerArtifactResolver,
    estimate_tokens,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_request import (
    ModelArtifactResolverRequest,
)
from omnimarket.nodes.node_context_artifact_resolver_compute.models.model_artifact_resolver_result import (
    EnumArtifactResolverStatus,
    ModelArtifactResolverResult,
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


def _request(*sources: ModelArtifactSource) -> ModelArtifactResolverRequest:
    return ModelArtifactResolverRequest(
        contract_hash=_CONTRACT_HASH,
        generated_at=_GENERATED_AT,
        sources=tuple(sources),
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


def test_whole_body_factor_resolves_one_artifact_with_provenance() -> None:
    body = "chains:\n  - name: registration"
    result = HandlerArtifactResolver().handle(
        _request(
            _source(
                EnumContextFactor.GOLDEN_CHAIN, body, source_name="golden_chains.yaml"
            ),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.factor is EnumContextFactor.GOLDEN_CHAIN
    assert artifact.content == body
    assert artifact.token_estimate == estimate_tokens(body)
    assert artifact.provenance is EnumContextPackProvenance.CURATED
    assert artifact.source_contract_hash == _CONTRACT_HASH
    assert artifact.source_file == "golden_chains.yaml"
    assert artifact.heading_path is None
    assert artifact.char_count == len(body)
    assert artifact.reason_selected == "whole_body"
    assert result.resolved_factors == ("golden_chain",)


def test_no_stub_placeholder_text_in_output() -> None:
    result = HandlerArtifactResolver().handle(
        _request(
            _source(EnumContextFactor.GOLDEN_CHAIN, "real golden chain text"),
            _source(EnumContextFactor.EXEMPLAR, "real exemplar code"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    for artifact in result.artifacts:
        assert "stub content for" not in artifact.content


# ---------------------------------------------------------------------------
# markdown section parsing: every section kept, none selected away
# ---------------------------------------------------------------------------


def test_sectioned_markdown_keeps_every_section() -> None:
    # Each section ~ 200 chars => ~50 tokens; the resolver keeps all four and
    # leaves selection to the pack builder.
    body = "\n".join(f"# Section {i}\n" + ("x" * 196) for i in range(1, 5))
    result = HandlerArtifactResolver().handle(
        _request(
            _source(
                EnumContextFactor.CLAUDE_MD,
                body,
                sectioned=True,
                source_name="CLAUDE.md",
            ),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert [a.heading_path for a in result.artifacts] == [
        (f"Section {i}",) for i in range(1, 5)
    ]
    assert all(a.reason_selected == "markdown_section" for a in result.artifacts)
    assert all(a.source_file == "CLAUDE.md" for a in result.artifacts)
    assert not any("selected" in w for w in result.warnings)


def test_sectioned_markdown_keeps_text_before_first_heading() -> None:
    body = "Preamble prose.\n\n# First\nbody one"
    result = HandlerArtifactResolver().handle(
        _request(_source(EnumContextFactor.CLAUDE_MD, body, sectioned=True))
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert [a.content for a in result.artifacts] == [
        "Preamble prose.",
        "# First\nbody one",
    ]
    assert result.artifacts[0].heading_path is None


def test_sectioned_markdown_without_headings_uses_whole_body() -> None:
    result = HandlerArtifactResolver().handle(
        _request(
            _source(
                EnumContextFactor.ARCHITECTURE_PATTERNS,
                "no headings here, just prose about nodes and handlers",
                sectioned=True,
            ),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert len(result.artifacts) == 1
    assert "no headings here" in result.artifacts[0].content
    assert result.artifacts[0].reason_selected == "whole_body"
    assert any("no headings" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# no assembly policy: no ordering, dedup, budget or pack hash
# ---------------------------------------------------------------------------


def test_artifacts_keep_source_order() -> None:
    # Precedence ordering is the pack builder's; the resolver keeps input order.
    result = HandlerArtifactResolver().handle(
        _request(
            _source(EnumContextFactor.CLAUDE_MD, "# C\nclaude md", sectioned=True),
            _source(EnumContextFactor.GOLDEN_CHAIN, "golden"),
            _source(EnumContextFactor.EXEMPLAR, "exemplar"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert result.resolved_factors == ("claude_md", "golden_chain", "exemplar")


def test_duplicate_content_is_not_deduplicated() -> None:
    result = HandlerArtifactResolver().handle(
        _request(
            _source(EnumContextFactor.EXEMPLAR, "same body", source_name="a.py"),
            _source(EnumContextFactor.EXEMPLAR, "same body", source_name="b.py"),
        )
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert [a.source_file for a in result.artifacts] == ["a.py", "b.py"]


def test_over_budget_source_is_resolved_not_rejected() -> None:
    # ~20000 tokens: the budget hard-reject is the pack builder's, not ours.
    huge = "x" * 80000
    result = HandlerArtifactResolver().handle(
        _request(_source(EnumContextFactor.CLAUDE_MD, huge))
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert result.artifacts[0].token_estimate == 20000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_budget", 16000),
        ("per_factor_token_budget", 3000),
        ("factor_precedence", ()),
        ("model_id", "resolver"),
    ],
)
def test_request_refuses_assembly_policy_inputs(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ModelArtifactResolverRequest.model_validate(
            {
                "contract_hash": _CONTRACT_HASH,
                "generated_at": _GENERATED_AT,
                "sources": (),
                field: value,
            }
        )


def test_result_carries_no_pack_hash_or_content_map() -> None:
    fields = set(ModelArtifactResolverResult.model_fields)
    assert "pack_hash" not in fields
    assert "artifact_content_map" not in fields
    assert "total_token_estimate" not in fields
    assert "failure_class" not in fields


# ---------------------------------------------------------------------------
# determinism + empty input
# ---------------------------------------------------------------------------


def test_resolution_is_deterministic() -> None:
    handler = HandlerArtifactResolver()
    req = _request(
        _source(EnumContextFactor.GOLDEN_CHAIN, "golden"),
        _source(EnumContextFactor.EXEMPLAR, "exemplar body"),
    )
    assert handler.handle(req) == handler.handle(req)


def test_empty_sources_fails() -> None:
    result = HandlerArtifactResolver().handle(_request())
    assert result.status is EnumArtifactResolverStatus.FAILED
    assert result.errors
    assert result.artifacts == ()


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
    sectioned = factor in (
        EnumContextFactor.CLAUDE_MD,
        EnumContextFactor.ARCHITECTURE_PATTERNS,
    )
    body = (
        f"# Head\nreal content for {factor.value}"
        if sectioned
        else f"real {factor.value}"
    )
    result = HandlerArtifactResolver().handle(
        _request(_source(factor, body, sectioned=sectioned))
    )
    assert result.status is EnumArtifactResolverStatus.OK
    assert result.resolved_factors == (factor.value,)
    assert result.artifacts[0].content.strip() != ""
