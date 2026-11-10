# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Count already-ruled question refusals and operator restatements per window.

The compaction measurement plan's live counterpart of "decisions dropped" is
two per-hour rates inside each session window that starts at a compaction
boundary and ends at the next boundary or the session end:

* refusals: FRICTION rows whose `guard` is `question_guard`, each placed in the
  one window that contains its timestamp (and names its session when the row
  does). A row that fits no window, or several, is reported unassigned with the
  reason, so every row is in exactly one of the two lists.
* restatements: an operator prompt inside the window whose text shares at least
  `restatement_overlap` of the word shingles of the quote of a ruling already in
  force at the boundary (stamped before the window starts), unless a RULING that
  amends that ruling is stamped within `amend_window_seconds` after the prompt.
  A quote shorter than one shingle never matches. A prompt is counted once.

Words are casefolded alphanumeric runs, so case and punctuation do not hide a
restatement. The handler reads no file, bus or model; callers pass typed rows.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..models.model_live_signal import (
    ModelBoundaryWindow,
    ModelLiveSignalRequest,
    ModelLiveSignalResult,
    ModelOperatorPrompt,
    ModelRestatement,
    ModelRulingRow,
    ModelUnassignedRefusal,
    ModelWindowSignal,
)

QUESTION_GUARD = "question_guard"
_WORD = re.compile(r"\w+")


def _shingles(text: str, size: int) -> frozenset[tuple[str, ...]]:
    words = _WORD.findall(text.casefold())
    return frozenset(
        tuple(words[index : index + size]) for index in range(len(words) - size + 1)
    )


class HandlerCompactionLiveSignal:
    """Read the live counterpart of S1 from typed ledger and prompt records."""

    def handle(self, request: ModelLiveSignalRequest) -> ModelLiveSignalResult:
        refusal_ids: dict[str, list[str]] = {w.window_id: [] for w in request.windows}
        unassigned: list[ModelUnassignedRefusal] = []
        question_rows = 0
        for row in request.guard_rows:
            if row.guard != QUESTION_GUARD:
                continue
            question_rows += 1
            candidates = [
                w
                for w in request.windows
                if w.start <= row.timestamp < w.end
                and (row.session_id is None or row.session_id == w.session_id)
            ]
            if len(candidates) == 1:
                refusal_ids[candidates[0].window_id].append(row.row_id)
            else:
                unassigned.append(
                    ModelUnassignedRefusal(
                        row_id=row.row_id,
                        reason="outside_windows"
                        if not candidates
                        else "ambiguous_windows",
                        candidate_window_ids=tuple(w.window_id for w in candidates),
                    )
                )
        signals = tuple(
            self._window_signal(window, tuple(refusal_ids[window.window_id]), request)
            for window in request.windows
        )
        return ModelLiveSignalResult(
            windows=signals,
            question_guard_rows=question_rows,
            assigned_rows=question_rows - len(unassigned),
            unassigned=tuple(unassigned),
            ignored_other_guard_rows=len(request.guard_rows) - question_rows,
        )

    def _window_signal(
        self,
        window: ModelBoundaryWindow,
        row_ids: tuple[str, ...],
        request: ModelLiveSignalRequest,
    ) -> ModelWindowSignal:
        in_force = [
            (ruling, _shingles(ruling.quote, request.shingle_size))
            for ruling in request.rulings
            if ruling.stamp < window.start
        ]
        restatements = tuple(
            restatement
            for prompt in request.prompts
            if prompt.session_id == window.session_id
            and window.start <= prompt.timestamp < window.end
            and (restatement := self._restatement(prompt, in_force, request))
        )
        hours = (window.end - window.start).total_seconds() / 3600
        return ModelWindowSignal(
            window_id=window.window_id,
            session_id=window.session_id,
            start=window.start,
            end=window.end,
            duration_hours=hours,
            refusal_row_ids=row_ids,
            refusal_count=len(row_ids),
            refusals_per_hour=len(row_ids) / hours,
            restatements=restatements,
            restatement_count=len(restatements),
            restatements_per_hour=len(restatements) / hours,
        )

    @staticmethod
    def _restatement(
        prompt: ModelOperatorPrompt,
        in_force: list[tuple[ModelRulingRow, frozenset[tuple[str, ...]]]],
        request: ModelLiveSignalRequest,
    ) -> ModelRestatement | None:
        prompt_shingles = _shingles(prompt.text, request.shingle_size)
        deadline: datetime = prompt.timestamp + timedelta(
            seconds=request.amend_window_seconds
        )
        stamps: list[datetime] = []
        for ruling, quote_shingles in in_force:
            if not quote_shingles:
                continue
            shared = len(quote_shingles & prompt_shingles)
            if shared / len(quote_shingles) < request.restatement_overlap:
                continue
            amended = any(
                prompt.timestamp <= other.stamp <= deadline
                and ruling.stamp in other.amends
                for other in request.rulings
            )
            if not amended:
                stamps.append(ruling.stamp)
        if not stamps:
            return None
        return ModelRestatement(
            prompt_id=prompt.prompt_id, matched_ruling_stamps=tuple(sorted(stamps))
        )
