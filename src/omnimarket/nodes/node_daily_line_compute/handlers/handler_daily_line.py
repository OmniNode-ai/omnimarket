# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render the daily line of rebuild plan section 11 (OMN-22925).

``Piece 1, code graph: UNKNOWN (timer not yet run), joined to piece 2, hosts, by J1: not yet run; claims with no
TERMINAL: N.``

Pure: no clock, no I/O. A source that was not read renders ``UNKNOWN`` for its part and the sentence keeps its shape; a
blank value is treated as not read, so no part of the line is ever an empty cell.
"""

from __future__ import annotations

from omnimarket.models.model_daily_line import (
    UNKNOWN,
    ModelDailyLinePart,
    ModelDailyLineRequest,
    ModelDailyLineResult,
)

PIECE1_UNREAD_DETAIL = "the floor record's piece 1 reading is not read"
J1_UNREAD = UNKNOWN
CLAIMS_UNREAD = UNKNOWN


def _words(text: str) -> str:
    """One line of single-spaced words: a newline or a run of spaces in a source never breaks the sentence."""
    return " ".join(text.split())


def _state(text: str) -> str:
    """A reading state as the floor draws it: upper case, with a space where the enum has an underscore."""
    return _words(text.replace("_", " ")).upper()


class HandlerDailyLine:
    """Handler for ``node_daily_line_compute``: the daily line from piece 1's reading, J1's state and the open claims."""

    def handle(self, request: ModelDailyLineRequest) -> ModelDailyLineResult:
        parts = (
            self._piece1(request),
            self._j1(request),
            self._claims(request),
        )
        piece1, j1, claims = (part.text for part in parts)
        line = (
            f"Piece 1, code graph: {piece1}, joined to piece 2, hosts, by J1: {j1}; "
            f"claims with no TERMINAL: {claims}."
        )
        return ModelDailyLineResult(
            line=line,
            parts=parts,
            unknown_parts=tuple(part.name for part in parts if not part.read),
        )

    @staticmethod
    def _piece1(request: ModelDailyLineRequest) -> ModelDailyLinePart:
        piece = request.piece1
        state = _state(piece.state) if piece is not None else ""
        if piece is None or not state:
            detail = _words(piece.detail) if piece is not None else ""
            return ModelDailyLinePart(
                name="piece1",
                text=f"{UNKNOWN} ({detail or PIECE1_UNREAD_DETAIL})",
                read=False,
            )
        detail = _words(piece.detail)
        return ModelDailyLinePart(
            name="piece1",
            text=f"{state} ({detail})" if detail else state,
            read=True,
        )

    @staticmethod
    def _j1(request: ModelDailyLineRequest) -> ModelDailyLinePart:
        state = _words(request.j1_state or "")
        if not state:
            return ModelDailyLinePart(name="j1", text=J1_UNREAD, read=False)
        return ModelDailyLinePart(name="j1", text=state, read=True)

    @staticmethod
    def _claims(request: ModelDailyLineRequest) -> ModelDailyLinePart:
        if request.open_claims is None:
            return ModelDailyLinePart(name="claims", text=CLAIMS_UNREAD, read=False)
        return ModelDailyLinePart(
            name="claims", text=str(request.open_claims), read=True
        )
