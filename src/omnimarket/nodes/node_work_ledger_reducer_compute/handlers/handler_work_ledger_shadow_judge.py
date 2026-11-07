# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer judge in shadow: its verdict, filed beside the live judge's (OMN-22915, L10).

Runs after the serve's file judge has answered a command, on the ledger text that already holds every
row the file judge accepted before it, so the shadow never depends on projection lag. It writes
nothing to the ledger: it returns the record that holds both verdicts and, when they differ, names how.
The judge is injected, so a test can break the guard and watch the difference list change.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_judge import (
    judge_row,
    verdict_difference,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
    ModelShadowJudgeRequest,
    ModelShadowVerdictRecord,
)

JudgeFn = Callable[[str, str, str], ModelJudgeVerdict]


class HandlerWorkLedgerShadowJudge:
    """Judges each row the live judge answered, and keeps the ledger text the next row is judged on."""

    def __init__(self, judge: JudgeFn = judge_row) -> None:
        self._judge = judge
        self._accepted_text = ""

    def handle(self, request: ModelShadowJudgeRequest) -> ModelShadowVerdictRecord:
        shadow = self._judge(request.row, request.existing, request.ledger_name)
        difference = verdict_difference(request.live, shadow, names=("live", "shadow"))
        return ModelShadowVerdictRecord(
            row_ref=request.row_ref,
            live=request.live,
            shadow=shadow,
            agrees=difference is None,
            difference=difference,
        )

    def observe(
        self, *, row_ref: str, row: str, ledger_name: str, live: ModelJudgeVerdict
    ) -> ModelShadowVerdictRecord:
        """Judge one row on the text of every row the live judge accepted before it, then apply it.

        A row the live judge accepted joins the shadow's text whatever the shadow said, so one
        difference does not turn every later row into a second one.
        """
        record = self.handle(
            ModelShadowJudgeRequest(
                row_ref=row_ref,
                row=row,
                existing=self._accepted_text,
                ledger_name=ledger_name,
                live=live,
            )
        )
        if live.verdict is EnumJudgeVerdict.ACCEPT:
            self._accepted_text += row.rstrip("\n") + "\n"
        return record
