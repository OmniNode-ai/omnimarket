# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B replay of a committed judged run as calibrated judged-acceptance events.

A committed ``judgments.jsonl`` and its ``summary.json`` hold the verdicts of one judge run.
``handle`` turns every item the matrix counted (the lines with a primary judge) into one
``ModelDelegationAcceptanceJudgedEvent``, with the primary judge's calibration taken from the
summary and the delegated call's own time as the call time. It reads no file, calls no model and
reads no clock: what a committed run does not record (tenant, tier of each model, the judge's
model version, the rubric hash, the time of judgment) is named by the request.

The result is all of the run's events or none of them. An item with no tier for its model, no
call time or no verdict from its primary judge, a repeated delegated call, a primary judge with no
calibration, or a count that disagrees with the summary's matrix refuses the whole run, so a
replay never publishes part of a run.
"""

from __future__ import annotations

from omnimarket.models.delegation_acceptance_judge.enum_acceptance_publish_status import (
    EnumAcceptancePublishStatus,
)
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_replay_refusal import (
    EnumAcceptanceReplayRefusal,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_issue import (
    ModelAcceptanceReplayIssue,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_request import (
    ModelAcceptanceReplayRequest,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_result import (
    ModelAcceptanceReplayResult,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_row import (
    ModelAcceptanceReplayRow,
)
from omnimarket.models.delegation_acceptance_judge.model_acceptance_replay_verdict import (
    ModelAcceptanceReplayVerdict,
)
from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
    ModelDelegationAcceptanceJudgedEvent,
)


def _primary_verdict(
    row: ModelAcceptanceReplayRow,
) -> ModelAcceptanceReplayVerdict | None:
    return row.judge_codex if row.primary == "codex" else row.judge_opus


class HandlerDelegationAcceptanceJudgedReplay:
    """Turn a committed judged run into its judged-acceptance events."""

    def handle(
        self, request: ModelAcceptanceReplayRequest
    ) -> ModelAcceptanceReplayResult:
        issues: list[ModelAcceptanceReplayIssue] = []
        calibration = getattr(
            request.summary.calibration, request.summary.primary_judge
        )
        if calibration is None:
            issues.append(
                ModelAcceptanceReplayIssue(
                    code=EnumAcceptanceReplayRefusal.NO_CALIBRATION_FOR_PRIMARY,
                    message=(
                        f"summary.json holds no calibration for the primary judge "
                        f"{request.summary.primary_judge!r}"
                    ),
                )
            )
        judged = [row for row in request.rows if row.primary is not None]
        skipped = len(request.rows) - len(judged)
        events: list[ModelDelegationAcceptanceJudgedEvent] = []
        seen: set[str] = set()
        for row in judged:
            verdict = _primary_verdict(row)
            tier = request.tier_by_model.get(row.model)
            correlation = str(row.correlation_id)
            if verdict is None:
                issues.append(
                    ModelAcceptanceReplayIssue(
                        code=EnumAcceptanceReplayRefusal.NO_PRIMARY_VERDICT,
                        item_id=row.item_id,
                        message=f"item {row.item_id} names primary {row.primary!r} but holds no verdict from it",
                    )
                )
            if not tier:
                issues.append(
                    ModelAcceptanceReplayIssue(
                        code=EnumAcceptanceReplayRefusal.NO_TIER_FOR_MODEL,
                        item_id=row.item_id,
                        message=f"no tier is named for delegated model {row.model!r}",
                    )
                )
            if row.timestamp is None:
                issues.append(
                    ModelAcceptanceReplayIssue(
                        code=EnumAcceptanceReplayRefusal.NO_CALL_TIME,
                        item_id=row.item_id,
                        message=f"item {row.item_id} records no call time",
                    )
                )
            if correlation in seen:
                issues.append(
                    ModelAcceptanceReplayIssue(
                        code=EnumAcceptanceReplayRefusal.DUPLICATE_CORRELATION_ID,
                        item_id=row.item_id,
                        message=f"delegated call {correlation} is judged twice in one run",
                    )
                )
            seen.add(correlation)
            if (
                verdict is None
                or not tier
                or row.timestamp is None
                or calibration is None
            ):
                continue
            events.append(
                ModelDelegationAcceptanceJudgedEvent(
                    correlation_id=row.correlation_id,
                    tenant_id=request.tenant_id,
                    delegated_model_key=row.model,
                    delegated_tier=tier,
                    task_type=row.task_type,
                    kind=row.kind,
                    call_time=row.timestamp,
                    judge_run_id=request.judge_run_id,
                    judge_model=request.judge_model,
                    judge_model_version=request.judge_model_version,
                    rubric_id=request.rubric_id,
                    rubric_version=request.rubric_version,
                    rubric_hash=request.rubric_hash,
                    calibration_run_id=request.calibration_run_id,
                    calibration_n=calibration.overall.n,
                    calibration_agreement=calibration.overall.agree,
                    calibration_kappa=calibration.overall.kappa,
                    accept=verdict.accept,
                    quality=verdict.quality,
                    failure_class=verdict.failure_class,
                    judged_at=request.judged_at,
                )
            )
        expected = sum(cell.n for cell in request.summary.matrix)
        if len(judged) != expected:
            issues.append(
                ModelAcceptanceReplayIssue(
                    code=EnumAcceptanceReplayRefusal.COUNT_DISAGREES_WITH_SUMMARY,
                    message=(
                        f"judgments.jsonl holds {len(judged)} judged items; "
                        f"summary.json counts {expected}"
                    ),
                )
            )
        if issues:
            return ModelAcceptanceReplayResult(
                status=EnumAcceptancePublishStatus.FAILED,
                issues=tuple(issues),
                skipped_unjudged_count=skipped,
            )
        return ModelAcceptanceReplayResult(
            status=EnumAcceptancePublishStatus.COMPLETED,
            events=tuple(events),
            skipped_unjudged_count=skipped,
        )
