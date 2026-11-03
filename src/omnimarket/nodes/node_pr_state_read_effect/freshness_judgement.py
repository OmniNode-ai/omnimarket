# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place a row is judged current or not.

Today the judgement is the row's own age against ``max_row_age_seconds``. It is
kept as a single typed function so that a judgement against a per-repository
watermark replaces this function and nothing else.
"""

from datetime import datetime

from omnimarket.models.pr_state.model_pr_state_freshness_judgement import (
    ModelPrStateFreshnessJudgement,
)
from omnimarket.models.pr_state.model_pr_state_read_policy import (
    ModelPrStateReadPolicy,
)
from omnimarket.models.pr_state.model_pr_state_row import ModelPrStateRow


def judge_row_freshness(
    row: ModelPrStateRow, now: datetime, policy: ModelPrStateReadPolicy
) -> ModelPrStateFreshnessJudgement:
    row_age_seconds = (now - row.observed_at).total_seconds()
    return ModelPrStateFreshnessJudgement(
        fresh=row_age_seconds <= policy.max_row_age_seconds,
        row_age_seconds=row_age_seconds,
    )
