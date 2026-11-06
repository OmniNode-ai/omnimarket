# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Accountability for an exhausted subject, independent of its worker lease."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLandingExhaustionOwner(BaseModel):
    """A named human or lane responsible for the next step after exhaustion.

    This assignment grants no claim, extra attempt or permission to act. Holds,
    worker caps, the engine ladder and cause budgets still apply.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["human", "lane"]
    name: str = Field(..., min_length=1, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
    source: Literal["policy", "claim"] = "policy"


class ModelLandingExhaustionConfig(BaseModel):
    """Contract configuration resolved at the composition boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exhaustion_owner: ModelLandingExhaustionOwner
