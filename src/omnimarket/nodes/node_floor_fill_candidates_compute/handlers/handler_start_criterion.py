# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse a ``Start when:`` line into prerequisites, and read each prerequisite against what the request carries.

The grammar is closed. A line is ``none`` (with any explanation after it), or prerequisites joined by ``and``, a comma,
a semicolon or ``&``:

* ``piece N reads WORKING`` (also ``is`` or ``are``, or ``pieces N and M read WORKING``): the served pieces list reads
  the piece WORKING inside its own reading bound;
* ``<repo>#<n> merged`` or ``<ticket id> merged``: the reference is on the request's merged list;
* ``M0 PASS``: the request carries the M0 verdict PASS.

A parenthesised aside after a prerequisite is dropped. Anything else, an operator go included, is unparseable: the task reads not ready with the cause named.
"""

from __future__ import annotations

import re
from datetime import timedelta

from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_candidates_compute.models.model_floor_fill_candidates import (
    parse_stamp,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models.model_start_criterion import (
    WORKING,
    M0Verdict,
    ModelPieceReading,
    ModelPiecesRecordRead,
    ModelStartCriterionParse,
    ModelStartPrerequisite,
    PiecesUnreadCause,
)

_LABEL = re.compile(
    r"^\s*(?:[-*>]\s*)*(?:\*\*)?start\s+when(?:\*\*)?\s*:\s*(?:\*\*)?", re.I
)
_NONE = re.compile(r"^none(?![\w-])", re.I)
_SEPARATOR = re.compile(r"\s*(?:[,;&]|\band\b)*\s*", re.I)
_PIECE = re.compile(
    r"pieces?\s+(?P<nums>\d+(?:\s*(?:,|\band\b|&)\s*\d+)*)\s+(?:reads?\s+|is\s+|are\s+)?WORKING\b",
    re.I,
)
_MERGED = re.compile(
    r"(?P<ref>[\w.-]+/[\w.-]+#\d+|[\w.-]+#\d+|[A-Za-z][A-Za-z0-9]*-\d+)\s+(?:is\s+|are\s+|has\s+)?merged\b",
    re.I,
)
_M0 = re.compile(
    r"M0\s+(?:reads?\s+|is\s+|has\s+a\s+)?PASS(?:ES)?(?:\s+verdict)?\b", re.I
)
_ASIDE = re.compile(r"\s*\([^()]*\)")
_NUMBER = re.compile(r"\d+")
_SHOWN = 40


def normalize_ref(ref: str) -> str:
    """A pull request reference without its owner (``owner/repo#n`` is ``repo#n``), a ticket id upper-cased."""
    ref = ref.strip()
    if "#" in ref:
        return ref.rpartition("/")[2].lower()
    return ref.upper()


def parse_start_when(text: str) -> ModelStartCriterionParse:
    """Read a ``Start when:`` line (with or without its label) into prerequisites, or name why it cannot be read."""
    body = _LABEL.sub("", text, count=1).strip()
    if not body:
        return _unparseable("empty line")
    if _NONE.match(body):
        return ModelStartCriterionParse()
    body = _ASIDE.sub("", body).strip()
    prerequisites: list[ModelStartPrerequisite] = []
    pos = 0
    while True:
        separator = _SEPARATOR.match(body, pos)
        pos = separator.end() if separator else pos
        if body[pos:].strip(" .") == "":
            break
        piece = _PIECE.match(body, pos)
        if piece:
            prerequisites.extend(
                ModelStartPrerequisite(kind="piece-working", piece=int(n))
                for n in _NUMBER.findall(piece.group("nums"))
            )
            pos = piece.end()
            continue
        merged = _MERGED.match(body, pos)
        if merged:
            prerequisites.append(
                ModelStartPrerequisite(
                    kind="merged", ref=normalize_ref(merged.group("ref"))
                )
            )
            pos = merged.end()
            continue
        m0 = _M0.match(body, pos)
        if m0:
            prerequisites.append(ModelStartPrerequisite(kind="m0-pass"))
            pos = m0.end()
            continue
        return _unparseable(f"cannot read {body[pos : pos + _SHOWN].strip()!r}")
    if not prerequisites:
        return _unparseable("no prerequisite named")
    return ModelStartCriterionParse(prerequisites=tuple(prerequisites))


def _unparseable(cause: str) -> ModelStartCriterionParse:
    return ModelStartCriterionParse(unparseable_cause=cause)


class PrerequisiteReader:
    """Read prerequisites against the pieces list, the merged references and the M0 verdict one request carries."""

    def __init__(
        self,
        *,
        now: str,
        pieces_record: str | None,
        merged_refs: tuple[str, ...],
        m0_verdict: M0Verdict,
    ) -> None:
        self.merged = {normalize_ref(ref) for ref in merged_refs}
        self.m0_verdict = m0_verdict
        self.pieces: dict[int, ModelPieceReading] = {}
        self.pieces_unread: PiecesUnreadCause = ""
        self._read_pieces(now, pieces_record)

    def _read_pieces(self, now: str, text: str | None) -> None:
        if text is None or not text.strip():
            self.pieces_unread = "absent"
            return
        try:
            record = ModelPiecesRecordRead.model_validate_json(text)
            age = parse_stamp(now) - parse_stamp(record.generated_at)
        except (ValidationError, ValueError):
            self.pieces_unread = "malformed"
            return
        if age > timedelta(seconds=record.freshness_max_age_s):
            self.pieces_unread = "old"
            return
        self.pieces = {p.piece: p for p in record.pieces}

    def unmet(self, prerequisite: ModelStartPrerequisite) -> str:
        """Why the prerequisite does not read true, or an empty string when it does."""
        if prerequisite.kind == "merged":
            return "" if prerequisite.ref in self.merged else "not merged"
        if prerequisite.kind == "m0-pass":
            return "" if self.m0_verdict == "PASS" else f"M0 reads {self.m0_verdict}"
        return self._unmet_piece(prerequisite.piece)

    def _unmet_piece(self, number: int | None) -> str:
        if self.pieces_unread:
            return f"pieces list unread ({self.pieces_unread})"
        reading = self.pieces.get(-1 if number is None else number)
        if reading is None:
            return "piece not on the pieces list"
        if reading.reading_state != WORKING:
            return f"reads {reading.reading_state or 'nothing'}"
        if reading.reading_age_s is None or reading.reading_max_age_s is None:
            return "reading has no age"
        if reading.reading_age_s > reading.reading_max_age_s:
            return "reading is old"
        return ""
