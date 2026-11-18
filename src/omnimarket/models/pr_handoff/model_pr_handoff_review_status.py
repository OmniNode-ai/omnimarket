# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review-gate commit statuses the caller read for a handed-off pull request."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_review_status import ModelReviewStatusRow


class ModelPrHandoffReviewStatus(BaseModel):
    """new_work is true when the PR's ticket or contract marks it as new work; ``statuses`` are the commit statuses
    the caller read for the PR (the live head's and any earlier head's). A new-work PR is handed off only with a
    success ``fable-review-gate`` status on the live head; the three refusals name missing, failing or another head.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    new_work: bool
    statuses: tuple[ModelReviewStatusRow, ...] = ()


__all__: list[str] = ["ModelPrHandoffReviewStatus"]
