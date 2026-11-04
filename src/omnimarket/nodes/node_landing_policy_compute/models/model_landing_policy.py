# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and verdict for the landing policy node (OMN-22192)."""

from pydantic import BaseModel, ConfigDict


class ModelLandingPolicyRequest(BaseModel):
    """One pull request author and the operator login the controller runs for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    login: str
    operator: str = ""
    is_bot: bool = False


class ModelLandingPolicyVerdict(BaseModel):
    """Whether the pull request lands like the operator's and whether the controller can land it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lands_like_operator: bool
    controller_lands: bool
    policy: str
