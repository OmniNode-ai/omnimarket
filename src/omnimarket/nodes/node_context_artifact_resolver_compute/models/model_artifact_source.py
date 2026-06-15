# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolved artifact source input for the context-artifact resolver.

A source carries the *already-read* content of one artifact (golden-chain
YAML text, an exemplar file's text, a CLAUDE.md / architecture-doc body, a
local-failure note) together with the factor it feeds and its provenance.

Archetype split (mirrors the P2-4 GuidanceSectionParser, OMN-12795):
  - File discovery / read is the EFFECT boundary; the caller reads the file
    and passes ``raw_content`` as a string.
  - This model and the resolver handler that consumes it are pure COMPUTE:
    no filesystem access, deterministic.

``source_name`` is a *logical* identifier (e.g. "golden_chains.yaml",
"CLAUDE.md#operating-rules") declared in contract config — never an absolute
filesystem path.  Absolute ``/Users/`` / ``/Volumes/`` strings are forbidden.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)
from pydantic import BaseModel, ConfigDict, Field


class ModelArtifactSource(BaseModel):
    """One pre-read artifact eligible to populate a factor's content.

    ``is_markdown_sectioned`` selects the resolution strategy:
      - True  -> the body is split into ATX-heading sections by the existing
                 GuidanceSectionParser and the highest-precedence sections are
                 selected up to the factor budget (claude_md, architecture
                 patterns).
      - False -> the whole body becomes a single artifact (golden_chain,
                 exemplar, local_failures).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor = Field(
        description="The context factor this source feeds."
    )
    source_name: str = Field(
        min_length=1,
        description=(
            "Logical artifact identifier declared in contract config "
            "(e.g. 'golden_chains.yaml'). Never an absolute filesystem path."
        ),
    )
    raw_content: str = Field(
        min_length=1,
        description=(
            "Already-read artifact body supplied by the EFFECT boundary. "
            "The resolver never opens files."
        ),
    )
    provenance: EnumContextPackProvenance = Field(
        description="Origin classification for the resolved content."
    )
    source_contract_hash: str = Field(
        min_length=1,
        description="SHA-256 of the source contract/config for provenance.",
    )
    is_markdown_sectioned: bool = Field(
        default=False,
        description=(
            "True -> split into heading sections and select within the factor "
            "budget (markdown guidance/architecture docs). "
            "False -> use the whole body as one artifact."
        ),
    )
    source_ticket_id: str | None = Field(
        default=None,
        description="Optional Linear ticket id (e.g. OMN-XXXX) for provenance.",
    )
    source_priority: int = Field(
        default=100,
        ge=0,
        description=(
            "Tie-break priority among multiple sources for the same factor; "
            "lower wins. Mirrors ModelContextPackArtifact.source_priority."
        ),
    )


__all__ = ["ModelArtifactSource"]
