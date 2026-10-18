# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The copy outcome for one supplied host."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelPrrpSnapshotHostOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    status: Literal["written", "failed", "skipped"]
    detail: str = ""
