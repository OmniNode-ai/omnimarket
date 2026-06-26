"""Pure guidance-file section parser: string in → ParsedSection list out.

Archetype: COMPUTE-eligible — zero filesystem I/O; callers (EFFECTs) are
responsible for reading the file and passing its content as a string.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_provenance import (
    EnumContextPackProvenance,
)

from omnimarket.pack.model_context_pack_artifact import (
    ModelContextPackArtifact,
)

# Matches ATX headings: one to six `#` chars followed by a space and heading text.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParsedSection:
    """A single section emitted by GuidanceSectionParser.

    All fields are derived purely from the input string — no I/O occurs.
    """

    source_file: str
    heading_path: tuple[str, ...]
    content: str
    content_hash: str
    char_count: int
    reason_selected: str

    def to_artifact(
        self,
        *,
        factor: EnumContextFactor,
        token_estimate: int,
        provenance: EnumContextPackProvenance,
        source_contract_hash: str,
        source_ticket_id: str | None = None,
        source_run_id: str | None = None,
        source_priority: int = 100,
    ) -> ModelContextPackArtifact:
        """Convert this section into a ModelContextPackArtifact.

        The caller supplies token_estimate (from their tokeniser), factor,
        provenance, and the contract hash — all of which vary by call site.
        """
        return ModelContextPackArtifact(
            factor=factor,
            content=self.content,
            token_estimate=token_estimate,
            provenance=provenance,
            source_artifact_hash=self.content_hash,
            source_ticket_id=source_ticket_id,
            source_contract_hash=source_contract_hash,
            source_run_id=source_run_id,
            source_priority=source_priority,
            source_file=self.source_file,
            heading_path=self.heading_path,
            char_count=self.char_count,
            reason_selected=self.reason_selected,
        )


@dataclass
class _HeadingSpan:
    """Internal span collected while scanning a document."""

    level: int
    title: str
    start: int  # char offset in the full document where the heading line begins


class GuidanceSectionParser:
    """Split a guidance file into one ParsedSection per heading boundary.

    Usage (EFFECT side — the parser itself never opens files)::

        raw = Path("CLAUDE.md").read_text()
        sections = GuidanceSectionParser().parse(raw, source_file="CLAUDE.md")
        artifacts = [
            s.to_artifact(
                factor=EnumContextFactor.CLAUDE_MD,
                token_estimate=estimate_tokens(s.content),
                provenance=EnumContextPackProvenance.CURATED,
                source_contract_hash=contract_hash,
            )
            for s in sections
        ]

    The parser is deterministic: identical inputs always produce identical
    outputs, making it safe to call inside a COMPUTE handler.
    """

    def parse(
        self,
        text: str,
        *,
        source_file: str,
        reason_selected: str = "heading_boundary",
    ) -> list[ParsedSection]:
        """Parse *text* into sections delimited by ATX headings.

        Args:
            text: Full content of a guidance file (caller reads the file).
            source_file: Logical path stored in each section for provenance;
                must not be empty but is never opened by this method.
            reason_selected: Free-form label stored verbatim on each section;
                defaults to ``"heading_boundary"``.

        Returns:
            Ordered list of ParsedSection, one per heading found.  An empty
            document or one with no headings returns an empty list.
        """
        if not text:
            return []

        spans: list[_HeadingSpan] = [
            _HeadingSpan(
                level=len(m.group(1)), title=m.group(2).strip(), start=m.start()
            )
            for m in _HEADING_RE.finditer(text)
        ]
        if not spans:
            return []

        sections: list[ParsedSection] = []
        for idx, span in enumerate(spans):
            end = spans[idx + 1].start if idx + 1 < len(spans) else len(text)
            raw_content = text[span.start : end].rstrip("\n")
            if not raw_content:
                continue

            heading_path = self._heading_path(spans, idx)
            sections.append(
                ParsedSection(
                    source_file=source_file,
                    heading_path=heading_path,
                    content=raw_content,
                    content_hash=_content_hash(raw_content),
                    char_count=len(raw_content),
                    reason_selected=reason_selected,
                )
            )

        return sections

    @staticmethod
    def _heading_path(spans: list[_HeadingSpan], idx: int) -> tuple[str, ...]:
        """Build the breadcrumb path for the heading at *idx*.

        Walks backwards through the preceding spans, collecting the nearest
        ancestor at each lower level, to reproduce the logical nesting path.
        """
        current = spans[idx]
        path: list[str] = [current.title]
        target_level = current.level - 1

        for prev in reversed(spans[:idx]):
            if target_level <= 0:
                break
            if prev.level == target_level:
                path.insert(0, prev.title)
                target_level -= 1

        return tuple(path)


__all__ = ["GuidanceSectionParser", "ParsedSection"]
