# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed event for node_pr_review_orchestrator (OMN-13212 / B2).

Carries the preserved ``ReviewVerdict`` output shape plus the terminal FSM phase
so the ``pr-review-bot-completed`` terminal event and the ``skill_mapping``
``result_model`` stay valid across the rebuild.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.review.pr_review_io import EnumFsmPhase, ReviewVerdict


class ModelPrReviewCompletedEvent(BaseModel):
    """Terminal event emitted when a PR review run completes (or fails)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_phase: EnumFsmPhase = Field(
        ..., description="Terminal FSM phase (DONE/FAILED)."
    )
    verdict: ReviewVerdict = Field(
        ..., description="The preserved ReviewVerdict result."
    )


__all__: list[str] = ["ModelPrReviewCompletedEvent"]
