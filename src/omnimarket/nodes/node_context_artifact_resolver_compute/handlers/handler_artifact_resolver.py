# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerArtifactResolver -- resolve pre-read sources into pack artifacts (COMPUTE).

Pure, deterministic, zero I/O.  Turns pre-read artifact sources into
``ModelContextPackArtifact`` entries with provenance, for the one canonical
assembler, node_context_pack_builder_compute, to select, budget and hash.

Architecture conformance:
  - COMPUTE: no filesystem / network access; the EFFECT boundary supplied
    ``raw_content`` for every source.
  - Primitives only (OMN-21137): section parsing and source resolution. This
    handler applies no section selection, no deduplication, no token budget
    and no pack hash -- those belong to the pack builder alone, so a source
    passed through here reaches the builder with every section intact.
  - Builds on the shared ``omnimarket.pack`` artifact model -- no cross-node
    reach-in into node_context_pack_builder_compute.
  - No hardcoded paths or topic literals; sources carry logical names declared
    in contract config.
  - Deterministic: identical inputs always produce identical artifacts, in
    source order then section order.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

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
from omnimarket.pack.model_context_pack_artifact import ModelContextPackArtifact

# heuristic_chars token estimation: ~4 chars per token (matches the pack-builder
# profile default token_estimation_method="heuristic_chars"). A non-empty body
# always estimates at least one token so a source can never be silently dropped
# for a zero estimate.
_CHARS_PER_TOKEN = 4

# Matches ATX headings: one to six `#` chars followed by a space and text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

_REASON_WHOLE_BODY = "whole_body"
_REASON_SECTION = "markdown_section"


def estimate_tokens(text: str) -> int:
    """Heuristic token estimate: ceil(len / 4), minimum 1 for non-empty text."""
    if not text:
        return 0
    return max(1, -(-len(text) // _CHARS_PER_TOKEN))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Section:
    """One parsed markdown section: its heading text (if any) and its body."""

    heading: str | None
    content: str


def _split_sections(text: str) -> list[_Section]:
    """Split markdown into ATX-heading sections; whole body if no headings.

    Pure: deterministic, no I/O. Each section spans from one heading to the
    next (or to end-of-document for the last heading). Non-blank text before
    the first heading is kept as a leading section with no heading, so no
    part of the body is lost.
    """
    if not text:
        return []
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [_Section(heading=None, content=text)]
    sections: list[_Section] = []
    preamble = text[: matches[0].start()].strip("\n")
    if preamble.strip():
        sections.append(_Section(heading=None, content=preamble))
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        content = text[match.start() : end].rstrip("\n")
        if content:
            sections.append(_Section(heading=match.group(2).strip(), content=content))
    return sections


def _artifact(
    source: ModelArtifactSource,
    section: _Section,
    reason: str,
) -> ModelContextPackArtifact:
    return ModelContextPackArtifact(
        factor=source.factor,
        content=section.content,
        token_estimate=estimate_tokens(section.content),
        provenance=source.provenance,
        source_artifact_hash=_content_hash(section.content),
        source_ticket_id=source.source_ticket_id,
        source_contract_hash=source.source_contract_hash,
        source_priority=source.source_priority,
        source_file=source.source_name,
        heading_path=(section.heading,) if section.heading is not None else None,
        char_count=len(section.content),
        reason_selected=reason,
    )


def _artifacts_for_source(
    source: ModelArtifactSource,
    warnings: list[str],
) -> list[ModelContextPackArtifact]:
    """Resolve one pre-read source into its artifacts, every section kept."""
    if not source.is_markdown_sectioned:
        whole = _Section(heading=None, content=source.raw_content)
        return [_artifact(source, whole, _REASON_WHOLE_BODY)]

    sections = _split_sections(source.raw_content)
    if not _HEADING_RE.search(source.raw_content):
        warnings.append(
            f"source '{source.source_name}' for factor "
            f"'{source.factor.value}' has no headings -- using whole body"
        )
        return [_artifact(source, section, _REASON_WHOLE_BODY) for section in sections]
    return [_artifact(source, section, _REASON_SECTION) for section in sections]


class HandlerArtifactResolver:
    """COMPUTE handler: resolve pre-read sources into pack-builder artifacts."""

    def handle(
        self,
        request: ModelArtifactResolverRequest,
    ) -> ModelArtifactResolverResult:
        if not request.sources:
            return ModelArtifactResolverResult(
                status=EnumArtifactResolverStatus.FAILED,
                errors=("no artifact sources supplied",),
            )

        warnings: list[str] = []
        artifacts: list[ModelContextPackArtifact] = []
        for source in request.sources:
            artifacts.extend(_artifacts_for_source(source, warnings))

        resolved_factors: list[str] = []
        for artifact in artifacts:
            if artifact.factor.value not in resolved_factors:
                resolved_factors.append(artifact.factor.value)

        return ModelArtifactResolverResult(
            status=EnumArtifactResolverStatus.OK,
            artifacts=tuple(artifacts),
            resolved_factors=tuple(resolved_factors),
            warnings=tuple(warnings),
        )


__all__ = ["HandlerArtifactResolver", "estimate_tokens"]
