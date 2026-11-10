# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine login a lane start draws from the host's Claude token pool, and where the pool is (OMN-23425).

A draw names the pool label and nothing else: no model here has a field that could carry a token value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: The label grammar of node_claude_token_pool_effect (its LABEL_RE), so a line that is not a label is not one.
LABEL_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class ModelEngineLoginPool(BaseModel):
    """Where a host keeps its Claude token pool: the overlay's ``remote_lane_start.login_pool_*`` keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The python module of the pool node, run as ``python -m <module> resolve`` on the host.
    module: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")
    #: The interpreter of the host's dispatch venv, relative to a registry root.
    python_relative: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    #: The registry roots to look under, in order.
    roots: tuple[str, ...] = Field(min_length=1)
    #: The longest one draw may take on the host.
    draw_timeout_seconds: float = Field(gt=0)


class ModelEngineLoginDraw(BaseModel):
    """What one draw found: the label of the first usable pool token, or why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str | None = Field(default=None, pattern=LABEL_PATTERN)
    #: ``drawn``, or ``none`` when the pool resolved nothing (no pool, empty, or every token limited or revoked).
    state: str = Field(pattern=r"^(drawn|none)$")
    detail: str = ""


__all__ = ["LABEL_PATTERN", "ModelEngineLoginDraw", "ModelEngineLoginPool"]
