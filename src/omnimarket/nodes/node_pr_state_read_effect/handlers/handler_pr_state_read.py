# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge the projection row of one pull request: fresh, stale, or absent."""

from omnimarket.models.pr_state.model_pr_state_read_request import (
    ModelPrStateReadRequest,
)
from omnimarket.models.pr_state.model_pr_state_read_result import (
    ModelPrStateReadAbsent,
    ModelPrStateReadFresh,
    ModelPrStateReadResult,
    ModelPrStateReadStale,
)
from omnimarket.nodes.node_pr_state_read_effect.freshness_judgement import (
    judge_row_freshness,
)


class HandlerPrStateRead:
    def handle(self, request: ModelPrStateReadRequest) -> ModelPrStateReadResult:
        matches = [
            row
            for row in request.page.rows
            if row.repo == request.repo
            and row.pr_number == request.pr_number
            and (request.head_sha is None or row.head_sha == request.head_sha)
        ]
        if not matches:
            return ModelPrStateReadResult(
                result=ModelPrStateReadAbsent(
                    repo=request.repo,
                    pr_number=request.pr_number,
                    window_truncated=request.page.truncated,
                )
            )
        row = max(matches, key=lambda candidate: candidate.observed_at)
        judgement = judge_row_freshness(row, request.now, request.policy)
        if not judgement.fresh:
            return ModelPrStateReadResult(
                result=ModelPrStateReadStale(
                    repo=row.repo,
                    pr_number=row.pr_number,
                    observed_at=row.observed_at,
                    row_age_seconds=judgement.row_age_seconds,
                    max_row_age_seconds=request.policy.max_row_age_seconds,
                )
            )
        return ModelPrStateReadResult(
            result=ModelPrStateReadFresh(
                row=row, row_age_seconds=judgement.row_age_seconds
            )
        )
