# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classification result emitted by is_pipeline_touching_pr (OMN-9577)."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_pipeline_touch_reason import EnumPipelineTouchReason


class ModelPipelineTouchClassification(BaseModel):
    """Result of evaluating whether a PR touches the data pipeline.

    `is_pipeline_touching` is the headline boolean dod_verify/trigger policy consumes;
    `reason` plus the matched-evidence lists make the decision auditable in receipts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    is_pipeline_touching: bool
    reason: EnumPipelineTouchReason
    matched_paths: tuple[str, ...] = Field(default=())
    matched_labels: tuple[str, ...] = Field(default=())
    contract_flag: bool = Field(default=False)
