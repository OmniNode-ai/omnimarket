# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the link history node."""

from pydantic import BaseModel, ConfigDict


class ModelLinkHistoryRequest(BaseModel):
    """This read's loss per pinged host (None: the ping printed no summary), the history file and the read time (epoch seconds)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    losses: dict[str, float | None]
    now: float


class ModelLinkHistoryResult(BaseModel):
    """Each host's earlier reads, newest first: the ones ``judge`` may count beside this read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    priors: dict[str, tuple[float, ...]]
