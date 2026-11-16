# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure classifier's answer."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_merge_override_alarm_effect.models.enum_merge_override_verdict import (
    EnumMergeOverrideVerdict,
)


class ModelMergeOverrideClassification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumMergeOverrideVerdict
    ruling_row_id: str | None = None
    reason: str | None = None
