# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the attach-on-demand activation (OMN-22909)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamCrossing,
)


class ModelAttachOnDemandRequest(BaseModel):
    """One on-demand seam: the command its node consumes and the result it must publish in time.

    The node is found by its command topic; nothing consumes that topic until the activation
    attaches the node.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    seam: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")
    command_topic: str = Field(min_length=1)
    result_topic: str = Field(min_length=1)
    consumer_group_prefix: str = Field(min_length=1)
    payload: dict[str, str | int | bool]
    error_keys: tuple[str, ...] = Field(min_length=1)
    deadline_seconds: float = Field(ge=0)


class ModelAttachOnDemandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    seam: str
    status: Literal["completed", "failed"]
    verdict: EnumReadingVerdict
    attached: bool
    detached: bool
    crossing: EnumSeamCrossing | None = None
    elapsed_ms: int | None = None
    failure: str | None = None


__all__ = ["ModelAttachOnDemandRequest", "ModelAttachOnDemandResult"]
