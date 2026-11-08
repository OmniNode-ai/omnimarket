# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard replay: recorded rows re-judged in order, with every changed verdict listed (OMN-22915, L11).

Each recorded row is judged against the ledger as it stood before it: the rows the live judge accepted
earlier, never the replay's own verdicts, so one changed verdict cannot cascade into later rows. A row
is a difference when its replayed verdict differs from the recorded one, or when both refuse under
different rules. The judge is injected so a deliberately altered guard can be shown to make the list
non-empty.

A window that begins in the middle of a HOLD carries the rows accepted before it (``prior_accepted``) and is
judged against them. A window whose RELEASE names a hold that neither those rows nor an earlier entry opened
is refused as incomplete: judging it against an empty ledger would list differences the guards never made.
"""

from __future__ import annotations

from omnimarket.handlers.ledger_row_grammar import ID_RE, parse_row
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
    verdict_difference,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_shadow_judge import (
    JudgeFn,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelGuardReplayRequest,
    ModelGuardReplayResult,
    ModelReplayDifference,
)


class WorkLedgerGuardReplayError(ValueError):
    """The recorded window cannot be replayed against the ledger it begins in."""


def _opened_ids(rows: list[str]) -> set[str]:
    ids: set[str] = set()
    for line in rows:
        row = parse_row(line)
        if row is not None and (value := row.token("id")):
            ids.add(value)
    return ids


def _unopened_release(row_text: str, opened: set[str]) -> str | None:
    """The hold id a RELEASE names that no row before it opened, or None."""
    row = parse_row(row_text)
    if row is None or row.row_type != "RELEASE":
        return None
    target = row.token("re")
    if target is not None and ID_RE.match(target) and target not in opened:
        return target
    return None


class WorkLedgerGuardReplay:
    def __init__(self, judge: JudgeFn = judge_row) -> None:
        self._judge = judge

    def handle(self, request: ModelGuardReplayRequest) -> ModelGuardReplayResult:
        accepted: list[str] = [row.rstrip("\n") for row in request.prior_accepted]
        opened = _opened_ids(accepted)
        differences: list[ModelReplayDifference] = []
        for position, entry in enumerate(request.entries, start=1):
            unopened = _unopened_release(entry.row, opened)
            if unopened is not None:
                raise WorkLedgerGuardReplayError(
                    f"replay window is incomplete: entry {position} ({entry.row_ref}) releases hold "
                    f"{unopened}, which no row before the window or in it opened; the window begins "
                    "mid-hold, so supply the rows accepted before it as prior_accepted"
                )
            opened |= _opened_ids([entry.row])
            replayed = self._judge(entry.row, "\n".join(accepted), request.ledger_name)
            difference = verdict_difference(
                entry.recorded, replayed, names=("recorded", "replayed")
            )
            if difference is not None:
                differences.append(
                    ModelReplayDifference(
                        position=position,
                        row_ref=entry.row_ref,
                        recorded=entry.recorded,
                        replayed=replayed,
                        difference=difference,
                    )
                )
            if entry.recorded.verdict is EnumJudgeVerdict.ACCEPT:
                accepted.append(entry.row.rstrip("\n"))
        stamps = [entry.at for entry in request.entries if entry.at is not None]
        return ModelGuardReplayResult(
            judged=len(request.entries),
            window_start=min(stamps) if stamps else None,
            window_end=max(stamps) if stamps else None,
            differences=tuple(differences),
            equivalent=not differences,
        )
