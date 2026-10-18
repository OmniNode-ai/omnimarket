# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The default reviewer: the shared pure rescue-patch review handler, called in process."""

from __future__ import annotations

from omnimarket.handlers.rescue_patch_review import (
    HandlerRescuePatchReview,
)
from omnimarket.models.rescue_patch_review import (
    ModelRescuePatchReviewRequest,
)

from ..models import ModelRescuePatchDecision


def review_patch_with_compute_node(
    *, patch_text: str, applies_to_base: bool, already_on_base: bool
) -> ModelRescuePatchDecision:
    result = HandlerRescuePatchReview().handle(
        ModelRescuePatchReviewRequest(
            patch_text=patch_text,
            applies_to_base=applies_to_base,
            already_on_base=already_on_base,
        )
    )
    return ModelRescuePatchDecision(
        verdict=result.verdict.value,
        is_discard=result.verdict.is_discard,
        reason=result.reason,
    )
