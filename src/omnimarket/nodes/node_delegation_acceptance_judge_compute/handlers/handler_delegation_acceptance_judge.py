# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure definition-B acceptance judge: render the blind rubric, check a reply, score a run.

No model call, no file read and no clock: a caller resolves the rubric, runs the judges and
hands back their replies. Same request, same result.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.check_reply import (
    check_reply,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.parse_rubric import (
    parse_rubric,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.render_batches import (
    render_batches,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.render_report import (
    render_report,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.score_run import (
    score_run,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_run_status import (
    EnumAcceptanceRunStatus,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_result import (
    ModelAcceptanceJudgeResult,
)


def _status(issues: int) -> EnumAcceptanceRunStatus:
    return EnumAcceptanceRunStatus.FAILED if issues else EnumAcceptanceRunStatus.PASSED


class HandlerDelegationAcceptanceJudge:
    """Render, check and score the blind acceptance judging of delegated output."""

    def handle(
        self, request: ModelAcceptanceJudgeRequest
    ) -> ModelAcceptanceJudgeResult:
        rubric = parse_rubric(request.rubric_yaml)
        version = rubric.rubric_version
        if request.operation is EnumAcceptanceOperation.RENDER:
            batches, probe_ids, dropped, sample = render_batches(
                request.items, request.seed, rubric, request.cell_cap
            )
            return ModelAcceptanceJudgeResult(
                operation=request.operation,
                rubric_version=version,
                status=EnumAcceptanceRunStatus.PASSED,
                batches=tuple(batches),
                excluded_probe_ids=tuple(probe_ids),
                dropped_item_ids=tuple(dropped),
                double_judge_item_ids=tuple(item.item_id for item in sample),
            )
        if request.operation is EnumAcceptanceOperation.CHECK:
            verdicts, issues = check_reply(
                request.reply_text, request.batch_item_ids, rubric
            )
            return ModelAcceptanceJudgeResult(
                operation=request.operation,
                rubric_version=version,
                status=_status(len(issues)),
                verdicts=tuple(verdicts),
                issues=tuple(issues),
            )
        agreement, rows, events, accept_rate, ok_rate, issues = score_run(
            request.items,
            request.primary_verdicts,
            request.secondary_verdicts,
            request.cell_events,
            rubric,
        )
        judged_ids = {item.item_id for item in request.items}
        joined = sum(
            r.receipt != "none" and r.item_id in judged_ids for r in request.receipts
        )
        return ModelAcceptanceJudgeResult(
            operation=request.operation,
            rubric_version=version,
            status=_status(len(issues)),
            issues=tuple(issues),
            agreement=agreement,
            matrix=tuple(rows),
            events_counted=events,
            volume_weighted_accept_rate=accept_rate,
            volume_terminal_ok_rate=ok_rate,
            receipts_joined=joined,
            report_markdown=render_report(
                version,
                issues,
                agreement,
                rows,
                events,
                accept_rate,
                ok_rate,
                sum(row.judged for row in rows),
                joined,
            ),
        )
