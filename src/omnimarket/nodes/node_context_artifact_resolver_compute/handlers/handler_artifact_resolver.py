# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerArtifactResolver -- materialise real artifact_content_map (COMPUTE).

Pure, deterministic, zero I/O.  Turns pre-read artifact sources into the
per-factor content map the ROI runner (OMN-12798) injects, reusing the two
existing authorities instead of reimplementing them:

  1. ``GuidanceSectionParser`` (OMN-12795) splits markdown guidance /
     architecture sources into heading sections; the resolver greedily selects
     the highest-precedence sections up to a per-factor token budget.
  2. ``HandlerContextPackBuilder`` enforces the 16k token budget hard-reject,
     the canonical factor precedence, and chunk dedup.  The resolver collapses
     the resulting pack's chunks per factor into ``artifact_content_map``.

Archetype conformance:
  - COMPUTE: no filesystem / network access; the EFFECT boundary supplied
    ``raw_content`` for every source.
  - No hardcoded paths or topic literals; sources carry logical names declared
    in contract config.
  - Deterministic: identical inputs always produce an identical content map.
"""

from __future__ import annotations

import hashlib

from omnibase_core.enums.enum_context_factor import EnumContextFactor

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
from omnimarket.nodes.node_context_pack_builder_compute.handlers.handler_context_pack_builder import (
    HandlerContextPackBuilder,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_artifact import (
    ModelContextPackArtifact,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_builder_request import (
    ModelContextPackBuilderRequest,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_pack_builder_result import (
    EnumContextPackBuilderStatus,
    ModelContextPackBuilderResult,
)
from omnimarket.nodes.node_context_pack_builder_compute.models.model_context_profile import (
    ModelContextProfile,
)
from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
    ParsedSection,
)

# heuristic_chars token estimation: ~4 chars per token (matches the pack-builder
# profile default token_estimation_method="heuristic_chars"). A non-empty body
# always estimates at least one token so a source can never be silently dropped
# for a zero estimate.
_CHARS_PER_TOKEN = 4

_TOKEN_ESTIMATION_METHOD = "heuristic_chars"


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate: ceil(len / 4), minimum 1 for non-empty text."""
    if not text:
        return 0
    return max(1, -(-len(text) // _CHARS_PER_TOKEN))


def _profile(request: ModelArtifactResolverRequest) -> ModelContextProfile:
    """Build the pack-builder profile from the resolver request.

    Every factor with a source is declared optional (not required) so that the
    pack builder never hard-fails on a missing factor -- the resolver resolves
    whatever sources it is given; arm-level required/optional policy is the
    runner's job, not the resolver's.
    """
    return ModelContextProfile(
        model_id=request.model_id,
        required_factors=(),
        optional_factors=request.factor_precedence,
        excluded_factors=(),
        factor_precedence=request.factor_precedence,
        token_budget=request.token_budget,
        token_estimation_method=_TOKEN_ESTIMATION_METHOD,
    )


def _select_sections(
    sections: list[ParsedSection],
    per_factor_token_budget: int,
) -> tuple[list[ParsedSection], int]:
    """Greedily select leading sections up to the per-factor token budget.

    Sections preserve document order (the parser's heading order). Selection is
    greedy in that order: include each section while the running token total
    stays within budget; stop at the first section that would overflow. At
    least one section is always included if the first fits, so a non-empty
    source never resolves to empty unless its very first section alone exceeds
    the budget.
    """
    selected: list[ParsedSection] = []
    running = 0
    for section in sections:
        cost = estimate_tokens(section.content)
        if selected and running + cost > per_factor_token_budget:
            break
        if not selected and cost > per_factor_token_budget:
            # First section alone overflows: take it anyway so the factor is not
            # silently empty; the overall pack-budget hard-reject still guards
            # the union arm downstream.
            selected.append(section)
            running += cost
            break
        selected.append(section)
        running += cost
    return selected, running


def _artifacts_for_source(
    source: ModelArtifactSource,
    per_factor_token_budget: int,
    warnings: list[str],
) -> tuple[ModelContextPackArtifact, ...]:
    """Build one or more pack artifacts from a single resolved source."""
    if not source.is_markdown_sectioned:
        content = source.raw_content
        return (
            ModelContextPackArtifact(
                factor=source.factor,
                content=content,
                token_estimate=estimate_tokens(content),
                provenance=source.provenance,
                source_artifact_hash=_content_hash(content),
                source_ticket_id=source.source_ticket_id,
                source_contract_hash=source.source_contract_hash,
                source_priority=source.source_priority,
                source_file=source.source_name,
            ),
        )

    parser = GuidanceSectionParser()
    sections = parser.parse(source.raw_content, source_file=source.source_name)
    if not sections:
        # No ATX headings: fall back to the whole body as one section-less
        # artifact so a markdown source without headings is not dropped.
        content = source.raw_content
        warnings.append(
            f"source '{source.source_name}' for factor "
            f"'{source.factor.value}' has no headings -- using whole body"
        )
        return (
            ModelContextPackArtifact(
                factor=source.factor,
                content=content,
                token_estimate=estimate_tokens(content),
                provenance=source.provenance,
                source_artifact_hash=_content_hash(content),
                source_ticket_id=source.source_ticket_id,
                source_contract_hash=source.source_contract_hash,
                source_priority=source.source_priority,
                source_file=source.source_name,
            ),
        )

    selected, _running = _select_sections(sections, per_factor_token_budget)
    if len(selected) < len(sections):
        warnings.append(
            f"source '{source.source_name}' for factor '{source.factor.value}': "
            f"selected {len(selected)}/{len(sections)} sections within "
            f"per-factor budget {per_factor_token_budget}"
        )
    return tuple(
        section.to_artifact(
            factor=source.factor,
            token_estimate=estimate_tokens(section.content),
            provenance=source.provenance,
            source_contract_hash=source.source_contract_hash,
            source_ticket_id=source.source_ticket_id,
            source_priority=source.source_priority,
        )
        for section in selected
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collapse_pack_to_map(
    result: ModelContextPackBuilderResult,
) -> tuple[dict[str, str], tuple[str, ...], int]:
    """Collapse a built pack's chunks into a per-factor content map.

    Multiple chunks for the same factor (e.g. several selected guidance
    sections) are joined in pack (precedence) order with a blank line between
    them -- the same shape the runner's _assemble_context_text expects for a
    single factor's section.
    """
    pack = result.context_pack
    if pack is None:
        return {}, (), 0

    per_factor_parts: dict[str, list[str]] = {}
    for chunk in pack.chunks:
        per_factor_parts.setdefault(chunk.factor.value, []).append(chunk.content)

    content_map = {
        factor_value: "\n\n".join(parts)
        for factor_value, parts in per_factor_parts.items()
    }
    resolved_factors = tuple(content_map.keys())
    return content_map, resolved_factors, pack.total_token_estimate


class HandlerArtifactResolver:
    """COMPUTE handler: resolve real per-factor content for the ROI runner."""

    def handle(
        self,
        request: ModelArtifactResolverRequest,
    ) -> ModelArtifactResolverResult:
        warnings: list[str] = []

        if not request.sources:
            return ModelArtifactResolverResult(
                status=EnumArtifactResolverStatus.FAILED,
                errors=("no artifact sources supplied",),
            )

        artifacts: list[ModelContextPackArtifact] = []
        for source in request.sources:
            artifacts.extend(
                _artifacts_for_source(
                    source,
                    request.per_factor_token_budget,
                    warnings,
                )
            )

        # Run through the existing budget/precedence authority. The pack builder
        # orders by factor precedence, dedups chunk ids, and hard-rejects when
        # the total exceeds token_budget (e.g. the full-guidance negative
        # control). The resolver never reimplements those rules.
        builder_request = ModelContextPackBuilderRequest(
            contract_hash=request.contract_hash,
            generated_at=request.generated_at,
            profile=_profile(request),
            artifacts=tuple(artifacts),
        )
        builder_result = HandlerContextPackBuilder().handle(builder_request)

        if builder_result.status is EnumContextPackBuilderStatus.FAILED:
            failure_class = (
                builder_result.failure_class.value
                if builder_result.failure_class is not None
                else None
            )
            return ModelArtifactResolverResult(
                status=EnumArtifactResolverStatus.FAILED,
                failure_class=failure_class,
                errors=builder_result.errors,
                warnings=tuple(warnings),
            )

        content_map, resolved_factors, total_tokens = _collapse_pack_to_map(
            builder_result
        )

        return ModelArtifactResolverResult(
            status=EnumArtifactResolverStatus.OK,
            artifact_content_map=content_map,
            resolved_factors=_ordered_resolved(
                resolved_factors, request.factor_precedence
            ),
            pack_hash=builder_result.pack_hash,
            total_token_estimate=total_tokens,
            warnings=tuple(warnings),
        )


def _ordered_resolved(
    resolved: tuple[str, ...],
    precedence: tuple[EnumContextFactor, ...],
) -> tuple[str, ...]:
    """Return resolved factor values in canonical precedence order."""
    order = {factor.value: index for index, factor in enumerate(precedence)}
    return tuple(sorted(resolved, key=lambda value: order.get(value, len(order))))


__all__ = ["HandlerArtifactResolver", "estimate_tokens"]
