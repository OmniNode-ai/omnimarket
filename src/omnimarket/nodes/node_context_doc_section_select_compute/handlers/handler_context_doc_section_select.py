# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select the document sections a profile's rules call for, verbatim (OMN-21126).

A rule fires when it is ``always_inject`` or the request's task kind is one of
its ``task_kinds``. Its section runs from the heading line to the next heading
of the same or a higher level, outside code fences, and is cut as whole lines,
without its trailing blank lines, so the entry text is a contiguous slice of the document at the pinned commit.
Plan excerpts need an active lifecycle row; a shape section that two documents
define differently is excluded from both. No token budget is applied and no
hash over the selection is computed: the builder owns both.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass

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

from omnimarket.nodes.node_context_doc_section_select_compute.models import (
    EnumDocSectionExclusionReason,
    EnumDocSectionSelectError,
    ModelCitedPath,
    ModelDocSectionDocument,
    ModelDocSectionExclusion,
    ModelDocSectionRule,
    ModelDocSectionSelectRequest,
    ModelDocSectionSelectResult,
    ModelPlanLifecycleRow,
    ModelSelectedDocSection,
)

__all__ = ["HandlerContextDocSectionSelect"]

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")
_BACKTICKED = re.compile(r"`([^`\s*]+)`")
_ACTIVE = "active"


@dataclass(frozen=True)
class _Heading:
    level: int
    title: str
    line: int


def _headings(lines: list[str]) -> list[_Heading]:
    """ATX headings outside fenced code blocks, in line order."""
    found: list[_Heading] = []
    fence: str | None = None
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        opened = _FENCE.match(line)
        if opened is not None:
            marker = opened.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is not None:
            continue
        match = _HEADING.match(line)
        if match is not None:
            found.append(_Heading(len(match.group(1)), match.group(2), index))
    return found


def _cited_paths(text: str, tree: frozenset[str]) -> tuple[ModelCitedPath, ...]:
    """Backticked repository paths in the excerpt, each checked against the tree."""
    seen: dict[str, bool] = {}
    for token in _BACKTICKED.findall(text):
        if "/" not in token or "://" in token or token in seen:
            continue
        prefix = token if token.endswith("/") else f"{token}/"
        seen[token] = token in tree or any(path.startswith(prefix) for path in tree)
    return tuple(ModelCitedPath(path=path, grounded=ok) for path, ok in seen.items())


@dataclass(frozen=True)
class _Candidate:
    tier_index: int
    rule: ModelDocSectionRule
    factor: EnumContextFactor
    start: int
    end: int
    text: str
    resolved_status: str | None


class HandlerContextDocSectionSelect:
    """Pure compute: documents, tier and rule data in, admitted sections out."""

    def handle(
        self, request: ModelDocSectionSelectRequest
    ) -> ModelDocSectionSelectResult:
        paths = [document.path for document in request.documents]
        if len(paths) != len(set(paths)):
            return ModelDocSectionSelectResult(
                status=EnumContextSourceStatus.UNAVAILABLE,
                error_code=EnumDocSectionSelectError.DUPLICATE_PATH,
                error_message="document paths must be unique",
            )

        documents = {document.path: document for document in request.documents}
        tier_index = {tier.tier_id: i for i, tier in enumerate(request.tiers)}
        tier_factor = {tier.tier_id: tier.factor for tier in request.tiers}
        lifecycle = {row.path: row for row in request.plan_lifecycle}
        tree = frozenset(request.tree_paths)
        excluded: list[ModelDocSectionExclusion] = []
        candidates: list[_Candidate] = []

        for rule in request.rules:
            if not (rule.always_inject or request.task_kind in rule.task_kinds):
                continue
            factor = tier_factor[rule.tier_id]
            source_ref = f"{rule.document_path}@{request.commit_sha}"

            def exclude(
                reason: EnumDocSectionExclusionReason,
                detail: str | None = None,
                *,
                rule: ModelDocSectionRule = rule,
                source_ref: str = source_ref,
            ) -> None:
                excluded.append(
                    ModelDocSectionExclusion(
                        tier_id=rule.tier_id,
                        path=rule.document_path,
                        heading=rule.heading,
                        reason=reason,
                        detail=detail,
                        source_ref=source_ref,
                    )
                )

            row: ModelPlanLifecycleRow | None = None
            if factor is EnumContextFactor.PLAN_EXCERPTS:
                row = lifecycle.get(rule.document_path)
                if row is None:
                    exclude(EnumDocSectionExclusionReason.PLAN_NOT_LISTED)
                    continue
                if row.status != _ACTIVE:
                    exclude(
                        EnumDocSectionExclusionReason.PLAN_NOT_ACTIVE,
                        f"lifecycle status {row.status}",
                    )
                    continue

            document = documents.get(rule.document_path)
            if document is None:
                exclude(EnumDocSectionExclusionReason.SECTION_MISSING, "no document")
                continue
            candidate = self._cut(
                document, rule, tier_index[rule.tier_id], factor, row, exclude
            )
            if candidate is not None:
                candidates.append(candidate)

        candidates = self._drop_ambiguous(candidates, request, excluded)
        candidates.sort(key=lambda c: (c.tier_index, c.rule.document_path, c.start))
        selected = [self._select(request, c, tree) for c in candidates]

        excluded.sort(
            key=lambda item: (tier_index[item.tier_id], item.path, item.heading)
        )
        if selected:
            status = EnumContextSourceStatus.RESOLVED
        elif any(
            item.reason is EnumDocSectionExclusionReason.AMBIGUOUS_DEFINITION
            for item in excluded
        ):
            status = EnumContextSourceStatus.AMBIGUOUS
        else:
            status = EnumContextSourceStatus.UNRESOLVED
        return ModelDocSectionSelectResult(
            status=status, selected=tuple(selected), excluded=tuple(excluded)
        )

    @staticmethod
    def _cut(
        document: ModelDocSectionDocument,
        rule: ModelDocSectionRule,
        tier_position: int,
        factor: EnumContextFactor,
        row: ModelPlanLifecycleRow | None,
        report: Callable[[EnumDocSectionExclusionReason, str | None], None],
    ) -> _Candidate | None:
        lines = document.file_text.splitlines(keepends=True)
        headings = _headings(lines)
        matches = [h for h in headings if h.title == rule.heading]
        if not matches:
            report(EnumDocSectionExclusionReason.SECTION_MISSING, "heading not found")
            return None
        if len(matches) > 1:
            report(
                EnumDocSectionExclusionReason.DUPLICATE_HEADING,
                f"heading at lines {[h.line for h in matches]}",
            )
            return None
        start = matches[0]
        end = next(
            (
                h.line
                for h in headings
                if h.line > start.line and h.level <= start.level
            ),
            len(lines),
        )
        while end > start.line + 1 and not lines[end - 1].strip():
            end -= 1
        text = "".join(lines[start.line : end])
        if text not in document.file_text:
            report(EnumDocSectionExclusionReason.NOT_CONTIGUOUS, None)
            return None
        return _Candidate(
            tier_index=tier_position,
            rule=rule,
            factor=factor,
            start=start.line,
            end=end,
            text=text,
            resolved_status=row.resolved_status if row is not None else None,
        )

    @staticmethod
    def _drop_ambiguous(
        candidates: list[_Candidate],
        request: ModelDocSectionSelectRequest,
        excluded: list[ModelDocSectionExclusion],
    ) -> list[_Candidate]:
        """Exclude every shape section whose heading two documents define differently."""
        groups: dict[tuple[str, str], list[_Candidate]] = {}
        for candidate in candidates:
            if candidate.factor is EnumContextFactor.CANONICAL_SHAPES:
                key = (candidate.rule.tier_id, candidate.rule.heading)
                groups.setdefault(key, []).append(candidate)
        ambiguous: set[int] = set()
        for group in groups.values():
            if len({c.text.strip() for c in group}) > 1:
                ambiguous.update(id(c) for c in group)
        kept: list[_Candidate] = []
        for candidate in candidates:
            if id(candidate) not in ambiguous:
                kept.append(candidate)
                continue
            excluded.append(
                ModelDocSectionExclusion(
                    tier_id=candidate.rule.tier_id,
                    path=candidate.rule.document_path,
                    heading=candidate.rule.heading,
                    reason=EnumDocSectionExclusionReason.AMBIGUOUS_DEFINITION,
                    detail="defined differently in another named document",
                    source_ref=f"{candidate.rule.document_path}@{request.commit_sha}",
                )
            )
        return kept

    @staticmethod
    def _select(
        request: ModelDocSectionSelectRequest,
        candidate: _Candidate,
        tree: frozenset[str],
    ) -> ModelSelectedDocSection:
        entry = ModelContextSourceEntry(
            factor=candidate.factor,
            text=candidate.text,
            source_ref=f"{candidate.rule.document_path}@{request.commit_sha}",
            span=ModelContextSourceSpan(
                unit=EnumContextSpanUnit.LINE,
                start=candidate.start,
                end=candidate.end,
            ),
            text_sha256=hashlib.sha256(candidate.text.encode("utf-8")).hexdigest(),
            status=EnumContextSourceStatus.RESOLVED,
            supersession_status=EnumContextSupersessionStatus.ACTIVE,
            verbatim_verified=True,
            paraphrase=False,
            as_of=request.as_of,
        )
        return ModelSelectedDocSection(
            tier_id=candidate.rule.tier_id,
            path=candidate.rule.document_path,
            heading=candidate.rule.heading,
            entry=entry,
            cited_paths=_cited_paths(candidate.text, tree),
            resolved_status=candidate.resolved_status,
        )
