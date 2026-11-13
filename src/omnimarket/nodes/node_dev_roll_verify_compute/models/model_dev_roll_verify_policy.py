# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every verifier value, read from the node's overlay. There is no default."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDevRollVerifyPolicy(BaseModel):
    """The overlay document of node_dev_roll_verify_compute.

    Decision D9 option (a): one runtime start per roll, with the hot patch /
    branch install built into that one start; this node counts the starts and
    verdicts the roll against the declared bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_starts_per_roll: int = Field(ge=1)
    runtime_lane: Literal["dev"]
