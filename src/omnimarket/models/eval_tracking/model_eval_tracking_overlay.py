# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of ``node_eval_tracking_schedule_compute`` (OMN-24177, plan task T13).

No key has a default: the cadence values are settings (plan D5, first values 7 and 28 days),
and an overlay that omits one refuses the load.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.eval_tracking.model_eval_tracking_record import (
    ModelEvalTrackingRecord,
)


class ModelEvalTrackingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    local_days: int = Field(ge=1, description="evals.tracking.local_days")
    glm_days: int = Field(ge=1, description="evals.tracking.glm_days")
    resend_seconds: int = Field(
        ge=60,
        description="How often one arm's current-window request is sent again. The decision "
        "keeps no state, so it names the window's run on every send; the trial driver drops "
        "a run id it already holds. A resend that follows a lost send or a crash is what "
        "starts the run once.",
    )
    record: ModelEvalTrackingRecord


__all__ = ["ModelEvalTrackingOverlay"]
