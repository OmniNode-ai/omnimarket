# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard replay: recorded rows re-judged in order, with every changed verdict listed (OMN-22915, L11).

Each recorded row is judged against the ledger as it stood before it: the rows the live judge accepted
earlier, never the replay's own verdicts, so one changed verdict cannot cascade into later rows. A row
is a difference when its replayed verdict differs from the recorded one, or when both refuse under
different rules. The judge is injected so a deliberately altered guard can be shown to make the list
non-empty.
"""

from __future__ import annotations

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


class WorkLedgerGuardReplay:
    def __init__(self, judge: JudgeFn = judge_row) -> None:
        self._judge = judge

    def handle(self, request: ModelGuardReplayRequest) -> ModelGuardReplayResult:
        accepted: list[str] = []
        differences: list[ModelReplayDifference] = []
        for position, entry in enumerate(request.entries, start=1):
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
