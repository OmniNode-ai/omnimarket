# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily work-ledger parity receipt row (OMN-20536, OMN-21885).

Pure: the receipt is one STATUS ledger row built from a parity report. OMN-21885 (plan task L1)
adds ``exact_kinds=`` (each of the 11 row kinds named ``KIND:yes`` or ``KIND:no``) and
``verb_diff=`` (a count per read verb, see ``parity_verbs``). Neither cell changes ``exact``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
    ModelWorkLedgerParityReport,
)
from omnimarket.nodes.node_projection_work_ledger.parity_verbs import format_verb_diff

BACKFILL_SOURCE = "onex-ledger-emit-backfill"
BACKFILL_SOURCES = frozenset({"onex-ledger-emit-backfill", "ledger-emit"})
RECEIPT_LANE = "work-ledger-parity"
RECEIPT_ACTOR = "script:work-ledger-parity"
# The 11 row kinds the receipt names (OMN-21885), in the order ``exact_kinds`` lists them.
ROW_KINDS = (
    "ACK",
    "CLAIM",
    "CORRECTION",
    "FRICTION",
    "HOLD",
    "MSG",
    "OPERATOR-CONSENT",
    "RELEASE",
    "RULING",
    "STATUS",
    "TERMINAL",
)
# The mismatch kinds that are row facts (missing, extra, text); state mismatches are entity facts.
_ROW_MISMATCH_KINDS = frozenset(
    {
        EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION,
        EnumParityMismatchKind.ROW_MISSING_IN_FILE,
        EnumParityMismatchKind.ROW_TEXT_DIFFERS,
        EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION,
        EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS,
    }
)


def backfilled_rows(report: ModelWorkLedgerParityReport) -> int:
    """Count projected rows emitted through either backfill path."""
    return sum(
        count
        for source, count in report.projection_sources.items()
        if source in BACKFILL_SOURCES
    )


def receipt_exact(report: ModelWorkLedgerParityReport) -> bool:
    """Phase 1's bar: exact parity and no backfilled row in the window."""
    return report.exact and backfilled_rows(report) == 0


def exact_kinds(report: ModelWorkLedgerParityReport) -> dict[str, bool]:
    """Each of the 11 row kinds: exact when the window held a row of it and none of the
    kind is missing, extra or text-mismatched. Pure.

    A row-level mismatch whose type is not one of the 11 (an extra projected row with no
    text, or a legacy type) cannot be charged to a kind, so it clears every kind."""
    broken = {
        m.row_type
        for m in report.mismatches
        if m.kind in _ROW_MISMATCH_KINDS and m.row_type in ROW_KINDS
    }
    unattributed = any(
        m.kind in _ROW_MISMATCH_KINDS and m.row_type not in ROW_KINDS
        for m in report.mismatches
    )
    return {
        kind: not unattributed
        and kind not in broken
        and report.file_rows_by_kind.get(kind, 0) > 0
        for kind in ROW_KINDS
    }


def receipt_row(
    report: ModelWorkLedgerParityReport,
    day: str,
    now: datetime,
    verb_diff: Mapping[str, int | None] | None = None,
) -> str:
    """The daily parity receipt for ``day``: one STATUS ledger row. Pure.

    ``verb_diff`` is the per-read-verb count of ``parity_verbs.run_verb_diff``; None means the
    verbs were not run, which the row says (``verb_diff=not-run``) rather than reading as zero."""
    kinds = Counter(m.kind for m in report.mismatches)
    missing = kinds[EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION]
    extra = kinds[EnumParityMismatchKind.ROW_MISSING_IN_FILE]
    state = sum(
        kinds[k]
        for k in (
            EnumParityMismatchKind.STATE_MISSING_IN_PROJECTION,
            EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS,
            EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS,
            EnumParityMismatchKind.STATE_RELATIONS_DIFFER,
        )
    )
    text = sum(
        kinds[k]
        for k in (
            EnumParityMismatchKind.ROW_TEXT_DIFFERS,
            EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION,
            EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS,
        )
    )
    unexplained = report.explain.unexplained if report.explain is not None else missing
    backfilled = 1 if backfilled_rows(report) > 0 else 0
    exact = "yes" if receipt_exact(report) else "no"
    cells = [
        f"{now:%Y-%m-%dT%H:%M:%SZ}", "STATUS", f"lane={RECEIPT_LANE}",
        f"actor={RECEIPT_ACTOR}", "model=none", f"day={day}",
        f"file_rows={report.file_rows}", f"projection_rows={report.projection_rows}",
        f"missing={missing}", f"extra={extra}", f"state_mismatches={state}",
        f"text_mismatches={text}",
        f"unexplained={unexplained}", f"backfilled={backfilled}", f"exact={exact}",
        "exact_kinds=" + ",".join(
            f"{kind}:{'yes' if ok else 'no'}" for kind, ok in exact_kinds(report).items()
        ),
        f"verb_diff={format_verb_diff(verb_diff)}",
        f"Work-ledger parity receipt for UTC day {day}",
    ]  # fmt: skip
    return " | ".join(cells)


__all__: list[str] = [
    "BACKFILL_SOURCE",
    "BACKFILL_SOURCES",
    "RECEIPT_ACTOR",
    "RECEIPT_LANE",
    "ROW_KINDS",
    "backfilled_rows",
    "exact_kinds",
    "receipt_exact",
    "receipt_row",
]
