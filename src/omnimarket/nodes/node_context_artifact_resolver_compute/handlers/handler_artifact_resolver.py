# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerArtifactResolver -- materialise real artifact_content_map (COMPUTE).

Pure, deterministic, zero I/O.  Turns pre-read artifact sources into the
per-factor content map the ROI runner (OMN-12798) injects.

Architecture conformance:
  - COMPUTE: no filesystem / network access; the EFFECT boundary supplied
    ``raw_content`` for every source.
  - Builds ONLY on shared omnibase_core pack types (EnumContextFactor,
    EnumContextPackProvenance, EnumContextPackFailure, ModelContextChunk,
    ModelContextPack, compute_chunk_id) -- NO cross-node model reach-in into
    node_context_pack_builder_compute (the omnimarket no-cross-node-reach-in
    gate forbids importing another node's private models).
  - The budget/precedence policy is the SAME deterministic policy the pack
    builder owns, expressed against the shared core types: factor-precedence
    ordering, then a 16k token-budget HARD-REJECT (EnumContextPackFailure.
    TOKEN_BUDGET_EXCEEDED) -- never silent truncation. This keeps the negative
    control (full-guidance) failing closed exactly as the pack builder would.
  - No hardcoded paths or topic literals; sources carry logical names declared
    in contract config.
  - Deterministic: identical inputs always produce an identical content map.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from omnibase_core.models.pack.model_context_chunk import ModelContextChunk
from omnibase_core.utils.util_context_pack import compute_chunk_id

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

# heuristic_chars token estimation: ~4 chars per token (matches the pack-builder
# profile default token_estimation_method="heuristic_chars"). A non-empty body
# always estimates at least one token so a source can never be silently dropped
# for a zero estimate.
_CHARS_PER_TOKEN = 4
_TOKEN_ESTIMATION_METHOD = "heuristic_chars"
_TOKENIZER_SOURCE = "heuristic"
_TOKENIZER_VERSION = "1.0.0"
_ESTIMATION_ACCURACY = "estimated"

# Matches ATX headings: one to six `#` chars followed by a space and text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate: ceil(len / 4), minimum 1 for non-empty text."""
    if not text:
        return 0
    return max(1, -(-len(text) // _CHARS_PER_TOKEN))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _ResolvedChunk:
    """An intermediate chunk pre-budget/precedence ordering."""

    factor: EnumContextFactor
    content: str
    token_estimate: int
    provenance: EnumContextPackProvenance
    source_artifact_hash: str
    source_contract_hash: str
    source_ticket_id: str | None
    source_priority: int


def _split_sections(text: str) -> list[str]:
    """Split markdown into ATX-heading sections; whole body if no headings.

    Pure: deterministic, no I/O. Each section spans from one heading to the
    next (or to end-of-document for the last heading).
    """
    if not text:
        return []
    starts = [m.start() for m in _HEADING_RE.finditer(text)]
    if not starts:
        return [text]
    sections: list[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        section = text[start:end].rstrip("\n")
        if section:
            sections.append(section)
    return sections


def _select_sections(
    sections: list[str],
    per_factor_token_budget: int,
) -> tuple[list[str], bool]:
    """Greedily select leading sections up to the per-factor token budget.

    Returns (selected, dropped_any). At least one section is always kept if the
    first fits; if the first section alone overflows it is kept anyway (so the
    factor is never silently empty) and the overall pack budget guards the union
    arm downstream.
    """
    selected: list[str] = []
    running = 0
    dropped = False
    for section in sections:
        cost = estimate_tokens(section)
        if selected and running + cost > per_factor_token_budget:
            dropped = True
            break
        selected.append(section)
        running += cost
        if not selected[:-1] and cost > per_factor_token_budget:
            # First section alone overflows: keep it, stop here.
            if len(sections) > 1:
                dropped = True
            break
    if len(selected) < len(sections):
        dropped = True
    return selected, dropped


def _resolved_chunks_for_source(
    source: ModelArtifactSource,
    per_factor_token_budget: int,
    warnings: list[str],
) -> list[_ResolvedChunk]:
    """Build resolved chunks from a single pre-read source."""
    if not source.is_markdown_sectioned:
        content = source.raw_content
        return [
            _ResolvedChunk(
                factor=source.factor,
                content=content,
                token_estimate=estimate_tokens(content),
                provenance=source.provenance,
                source_artifact_hash=_content_hash(content),
                source_contract_hash=source.source_contract_hash,
                source_ticket_id=source.source_ticket_id,
                source_priority=source.source_priority,
            )
        ]

    sections = _split_sections(source.raw_content)
    if len(sections) <= 1 and (
        not sections or not _HEADING_RE.search(source.raw_content)
    ):
        warnings.append(
            f"source '{source.source_name}' for factor "
            f"'{source.factor.value}' has no headings -- using whole body"
        )

    selected, dropped = _select_sections(sections, per_factor_token_budget)
    if dropped:
        warnings.append(
            f"source '{source.source_name}' for factor '{source.factor.value}': "
            f"selected {len(selected)}/{len(sections)} sections within "
            f"per-factor budget {per_factor_token_budget}"
        )
    return [
        _ResolvedChunk(
            factor=source.factor,
            content=section,
            token_estimate=estimate_tokens(section),
            provenance=source.provenance,
            source_artifact_hash=_content_hash(section),
            source_contract_hash=source.source_contract_hash,
            source_ticket_id=source.source_ticket_id,
            source_priority=source.source_priority,
        )
        for section in selected
    ]


def _to_context_chunk(resolved: _ResolvedChunk) -> ModelContextChunk:
    """Promote a resolved chunk to the shared core ModelContextChunk."""
    return ModelContextChunk(
        chunk_id=compute_chunk_id(resolved.factor.value, resolved.content),
        factor=resolved.factor,
        content=resolved.content,
        token_estimate=resolved.token_estimate,
        token_estimation_method=_TOKEN_ESTIMATION_METHOD,
        tokenizer_source=_TOKENIZER_SOURCE,
        tokenizer_version=_TOKENIZER_VERSION,
        estimation_accuracy=_ESTIMATION_ACCURACY,
        provenance=resolved.provenance,
        source_artifact_hash=resolved.source_artifact_hash,
        source_ticket_id=resolved.source_ticket_id,
        source_contract_hash=resolved.source_contract_hash,
        source_run_id=None,
    )


def _ordered_chunks(
    chunks: list[ModelContextChunk],
    precedence: tuple[EnumContextFactor, ...],
) -> list[ModelContextChunk]:
    """Order chunks by factor precedence, then source priority, deterministically.

    This is the same precedence policy node_context_pack_builder_compute owns,
    expressed against the shared core ModelContextChunk so no cross-node model
    import is needed.
    """
    order = {factor: index for index, factor in enumerate(precedence)}
    return sorted(
        chunks,
        key=lambda c: (
            order.get(c.factor, len(order)),
            c.source_artifact_hash,
            c.chunk_id,
        ),
    )


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

        resolved: list[_ResolvedChunk] = []
        for source in request.sources:
            resolved.extend(
                _resolved_chunks_for_source(
                    source,
                    request.per_factor_token_budget,
                    warnings,
                )
            )

        chunks = [_to_context_chunk(rc) for rc in resolved]

        # Dedup by chunk_id (same factor+content collapses to one chunk),
        # mirroring the pack builder's intra-pack dedup.
        seen: set[str] = set()
        deduped: list[ModelContextChunk] = []
        for chunk in chunks:
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            deduped.append(chunk)

        ordered = _ordered_chunks(deduped, request.factor_precedence)

        # Budget HARD-REJECT -- the same gate the pack builder enforces. Never
        # silently truncate; the full-guidance negative control fails closed.
        total_tokens = sum(c.token_estimate for c in ordered)
        if total_tokens > request.token_budget:
            return ModelArtifactResolverResult(
                status=EnumArtifactResolverStatus.FAILED,
                failure_class=EnumContextPackFailure.TOKEN_BUDGET_EXCEEDED.value,
                errors=(
                    f"token estimate {total_tokens} exceeds budget "
                    f"{request.token_budget}",
                ),
                warnings=tuple(warnings),
            )

        content_map, resolved_factors = _collapse_to_map(ordered)
        pack_hash = _pack_hash(request, ordered)

        return ModelArtifactResolverResult(
            status=EnumArtifactResolverStatus.OK,
            artifact_content_map=content_map,
            resolved_factors=resolved_factors,
            pack_hash=pack_hash,
            total_token_estimate=total_tokens,
            warnings=tuple(warnings),
        )


def _collapse_to_map(
    ordered: list[ModelContextChunk],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Collapse ordered chunks into a per-factor content map.

    Multiple chunks for the same factor (e.g. several selected guidance
    sections) join in precedence order with a blank line between them -- the
    shape the runner's _build_context_pack expects for a single factor.
    """
    per_factor: dict[str, list[str]] = {}
    factor_order: list[str] = []
    for chunk in ordered:
        key = chunk.factor.value
        if key not in per_factor:
            per_factor[key] = []
            factor_order.append(key)
        per_factor[key].append(chunk.content)
    content_map = {k: "\n\n".join(v) for k, v in per_factor.items()}
    return content_map, tuple(factor_order)


def _pack_hash(
    request: ModelArtifactResolverRequest,
    ordered: list[ModelContextChunk],
) -> str:
    payload = {
        "contract_hash": request.contract_hash,
        "model_id": request.model_id,
        "chunks": [c.model_dump(mode="json") for c in ordered],
    }

    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "pack_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


__all__ = ["HandlerArtifactResolver", "estimate_tokens"]
