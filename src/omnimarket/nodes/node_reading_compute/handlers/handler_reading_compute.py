# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reading: a pure function of ledger rows in a window that closed at a fixed time.

``handle(ModelReadingComputeRequest) -> ModelReadingComputeResult``. It reads no clock, file,
network or environment. The rules (plan section 12, Gates):

* The evidence of a source is the rows it selects whose stamp lies in
  ``[window_end - max_age, window_end)`` and that were ingested no later than the cutoff
  (window end plus the close delay): the input manifest is frozen at the cutoff.
* Identical duplicate rows (same key, same text) are dropped. A duplicate key with different
  text fails the window: the reading is NOT_WORKING, outcome ``window-failed``.
* A source with no row inside its ``max_age``, or whose newest row is older, is stale: NOT_WORKING.
  Stale is a failure, not a warning.
* A source that could not be read makes the reading UNKNOWN (outcome ``source-unreadable``).
  A piece with no producer today reads UNKNOWN with its reason, never blank.
* The row key is piece, window start, order hash, overlay hash and revision. The content hash
  covers what the reading says and not when it was run, so a replay of the same inputs is equal.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    EnumSourceMode,
    ModelLedgerEvidenceRow,
    ModelReadingComputeRequest,
    ModelReadingComputeResult,
    ModelReadingSource,
    ModelReadingSourceRows,
)

_KEY_LENGTH = 24
_TOKEN_LIMIT = 120
_NOT_TOKEN = re.compile(r"[^a-z0-9]+")


def stamp_text(moment: datetime) -> str:
    """The ledger's stamp form, UTC to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reason_token(text: str) -> str:
    """A free-text reason as one cell-safe token."""
    return _NOT_TOKEN.sub("-", text.lower()).strip("-")[:_TOKEN_LIMIT] or "unspecified"


def row_key(
    piece: int,
    window_start: datetime,
    order_hash: str,
    overlay_hash: str,
    revision: str,
) -> str:
    """Piece, window start, order hash, overlay hash and revision, as one key."""
    joined = "|".join(
        (str(piece), stamp_text(window_start), order_hash, overlay_hash, revision)
    )
    return _sha(joined)[:_KEY_LENGTH]


class _Gathered:
    """The evidence of one source after the cutoff, the window and the duplicate rules."""

    def __init__(self) -> None:
        self.rows: list[ModelLedgerEvidenceRow] = []
        self.conflicts: list[str] = []


def _gather(
    source: ModelReadingSource,
    supplied: ModelReadingSourceRows,
    window_end: datetime,
    cutoff: datetime,
) -> _Gathered:
    start = window_end - timedelta(seconds=source.max_age_seconds)
    kept: dict[str, ModelLedgerEvidenceRow] = {}
    gathered = _Gathered()
    for row in supplied.rows:
        if not (start <= row.stamp < window_end):
            continue
        if row.ingested_at is not None and row.ingested_at > cutoff:
            continue
        if not source.selects(row):
            continue
        earlier = kept.get(row.key)
        if earlier is None:
            kept[row.key] = row
        elif earlier.raw != row.raw:
            gathered.conflicts.append(_sha(row.key)[:12])
        # an identical duplicate is dropped
    gathered.rows = sorted(kept.values(), key=lambda r: (r.stamp, r.key))
    return gathered


def _judge(
    source: ModelReadingSource, rows: list[ModelLedgerEvidenceRow], window_end: datetime
) -> str | None:
    """None when the source passes, else the reason code."""
    if not rows:
        return f"stale-source:{source.source_id}:no-row-in-{source.max_age_seconds}s"
    if source.mode is EnumSourceMode.NEWEST:
        newest = rows[-1]
        age = int((window_end - newest.stamp).total_seconds())
        if age > source.max_age_seconds:
            return f"stale-source:{source.source_id}:age-{age}s-over-{source.max_age_seconds}s"
        if not source.passes(newest):
            return f"source-fail:{source.source_id}"
        return None
    passing = [row for row in rows if source.passes(row)]
    if len(passing) < source.min_rows:
        return f"below-minimum:{source.source_id}:{len(passing)}-of-{source.min_rows}"
    return None


class HandlerReadingCompute:
    """Compute one piece's reading for one closed window."""

    def handle(self, request: ModelReadingComputeRequest) -> ModelReadingComputeResult:
        if (
            request.cutoff < request.window_end
            or request.window_end <= request.window_start
        ):
            raise ValueError(
                "a window ends after it starts and its cutoff is not before its end"
            )
        spec = request.spec
        key = row_key(
            spec.piece,
            request.window_start,
            request.order_hash,
            request.overlay_hash,
            request.revision,
        )

        def result(
            verdict: EnumReadingVerdict,
            outcome: EnumReadingOutcome,
            reasons: list[str],
            manifest: list[str],
        ) -> ModelReadingComputeResult:
            manifest_hash = _sha("\n".join(sorted(manifest)))
            ordered = tuple(sorted(reasons))
            content = json.dumps(
                {
                    "verdict": verdict.value,
                    "outcome": outcome.value,
                    "reasons": ordered,
                    "manifest": manifest_hash,
                    "basis": spec.basis,
                },
                sort_keys=True,
            )
            return ModelReadingComputeResult(
                piece=spec.piece,
                name=spec.name,
                verdict=verdict,
                outcome=outcome,
                reasons=ordered,
                basis=spec.basis,
                window_start=request.window_start,
                window_end=request.window_end,
                order_hash=request.order_hash,
                overlay_hash=request.overlay_hash,
                revision=request.revision,
                manifest_hash=manifest_hash,
                rows_read=len(manifest),
                row_key=key,
                content_hash=_sha(content)[:_KEY_LENGTH],
                injected_fault=spec.injected_fault,
            )

        if not spec.readable_today:
            none_yet = f"no-producer:{reason_token(spec.unknown_reason or '')}"
            return result(
                EnumReadingVerdict.UNKNOWN, EnumReadingOutcome.READ, [none_yet], []
            )

        supplied = {item.source_id: item for item in request.sources}
        unreadable: list[str] = []
        gathered: dict[str, _Gathered] = {}
        for source in spec.sources:
            item = supplied.get(source.source_id)
            if item is None:
                unreadable.append(f"source-unreadable:{source.source_id}:not-supplied")
            elif item.error is not None:
                unreadable.append(
                    f"source-unreadable:{source.source_id}:{reason_token(item.error)}"
                )
            else:
                gathered[source.source_id] = _gather(
                    source, item, request.window_end, request.cutoff
                )
        if unreadable:
            return result(
                EnumReadingVerdict.UNKNOWN,
                EnumReadingOutcome.SOURCE_UNREADABLE,
                unreadable,
                [],
            )

        manifest = [
            f"{source.source_id}|{row.key}|{_sha(row.raw)}"
            for source in spec.sources
            for row in gathered[source.source_id].rows
        ]
        conflicts = [
            f"conflicting-duplicate:{source.source_id}:{digest}"
            for source in spec.sources
            for digest in gathered[source.source_id].conflicts
        ]
        if conflicts:
            return result(
                EnumReadingVerdict.NOT_WORKING,
                EnumReadingOutcome.WINDOW_FAILED,
                conflicts,
                manifest,
            )
        failures = [
            reason
            for source in spec.sources
            if (
                reason := _judge(
                    source, gathered[source.source_id].rows, request.window_end
                )
            )
            is not None
        ]
        if failures:
            return result(
                EnumReadingVerdict.NOT_WORKING,
                EnumReadingOutcome.READ,
                failures,
                manifest,
            )
        return result(
            EnumReadingVerdict.WORKING, EnumReadingOutcome.READ, ["ok"], manifest
        )
