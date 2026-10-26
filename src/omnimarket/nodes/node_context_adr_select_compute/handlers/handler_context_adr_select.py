# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select the ADRs a profile's topics call for, verbatim from their bodies (OMN-21126).

Selection is ``status: accepted``, no ``superseded_by``, and at least one
requested topic among the ADR's ``topics`` tags, in path order. Invalid
frontmatter and empty bodies are excluded regardless of topic. In-topic
superseded and non-accepted ADRs are listed as excluded with their status and
successors. Each admitted entry's text is cut whole after the closing
frontmatter delimiter at the pinned commit. No token budget is applied and no
hash over the selection is computed: the builder owns both.
"""

from __future__ import annotations

import hashlib

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)
from pydantic import ValidationError

from omnimarket.nodes.node_context_adr_select_compute.models import (
    EnumAdrExclusionReason,
    EnumAdrSelectError,
    ModelAdrExclusion,
    ModelAdrFrontmatter,
    ModelAdrSelectRequest,
    ModelAdrSelectResult,
    ModelSelectedAdr,
)

__all__ = ["HandlerContextAdrSelect"]


def _frontmatter(lines: list[str]) -> tuple[ModelAdrFrontmatter, int]:
    """Validate the delimited header and return its closing line index."""
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise ValueError("ADR has no opening frontmatter delimiter")
    close = next(
        (
            index
            for index in range(1, len(lines))
            if lines[index].rstrip("\r\n") == "---"
        ),
        None,
    )
    if close is None:
        raise ValueError("ADR has no closing frontmatter delimiter")
    loaded = yaml.safe_load("".join(lines[1:close]))
    if not isinstance(loaded, dict):
        raise ValueError("ADR frontmatter is not a mapping")
    return ModelAdrFrontmatter.model_validate(loaded), close


class HandlerContextAdrSelect:
    """Pure compute: document text and topics in, admitted ADRs and exclusions out."""

    def handle(self, request: ModelAdrSelectRequest) -> ModelAdrSelectResult:
        paths = [document.path for document in request.documents]
        if len(paths) != len(set(paths)):
            return ModelAdrSelectResult(
                status=EnumContextSourceStatus.UNAVAILABLE,
                error_code=EnumAdrSelectError.DUPLICATE_PATH,
                error_message="document paths must be unique",
            )

        wanted = set(request.topics)
        selected: list[ModelSelectedAdr] = []
        excluded: list[ModelAdrExclusion] = []
        for document in sorted(request.documents, key=lambda item: item.path):
            source_ref = f"{document.path}@{request.commit_sha}"
            lines = document.file_text.splitlines(keepends=True)
            try:
                record, close = _frontmatter(lines)
            except (yaml.YAMLError, ValidationError, ValueError) as exc:
                excluded.append(
                    ModelAdrExclusion(
                        path=document.path,
                        reason=EnumAdrExclusionReason.INVALID_FRONTMATTER,
                        detail=str(exc),
                        source_ref=source_ref,
                    )
                )
                continue
            body = "".join(lines[close + 1 :])
            reason: EnumAdrExclusionReason | None = None
            if not body:
                reason = EnumAdrExclusionReason.EMPTY_BODY
            elif not wanted.intersection(record.topics):
                continue
            elif record.superseded_by:
                reason = EnumAdrExclusionReason.SUPERSEDED
            elif record.status != "accepted":
                reason = EnumAdrExclusionReason.NOT_ACCEPTED
            if reason is not None:
                excluded.append(
                    ModelAdrExclusion(
                        path=document.path,
                        reason=reason,
                        adr_id=record.adr_id,
                        status=record.status,
                        superseded_by=record.superseded_by,
                        source_ref=source_ref,
                    )
                )
                continue
            entry = ModelContextSourceEntry(
                factor=EnumContextFactor.ADRS,
                text=body,
                source_ref=source_ref,
                span=ModelContextSourceSpan(
                    unit=EnumContextSpanUnit.LINE, start=close + 1, end=len(lines)
                ),
                text_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                status=EnumContextSourceStatus.RESOLVED,
                supersession_status=EnumContextSupersessionStatus.ACTIVE,
                verbatim_verified=True,
                paraphrase=False,
                as_of=request.as_of,
            )
            selected.append(
                ModelSelectedAdr(
                    path=document.path,
                    adr_id=record.adr_id,
                    title=record.title,
                    topics=record.topics,
                    entry=entry,
                )
            )
        status = (
            EnumContextSourceStatus.RESOLVED
            if selected
            else EnumContextSourceStatus.UNRESOLVED
        )
        return ModelAdrSelectResult(
            status=status, selected=tuple(selected), excluded=tuple(excluded)
        )
