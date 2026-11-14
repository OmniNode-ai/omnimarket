# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one overlay field of the roll gate that the deploy effect reads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelDevRollGateMode(BaseModel):
    """``mode`` of node_dev_roll_gate_effect's overlay; every other field is the gate's own."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    mode: Literal["off", "live"]
