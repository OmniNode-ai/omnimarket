# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The engine login a lane start draws from the host's Claude token pool, and where the pool is (OMN-23425).

A draw names the pool label and nothing else: no model here has a field that could carry a token value.
"""

from __future__ import annotations

from typing import Annotated

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
    #: ``drawn``, or ``none`` when the pool resolved nothing (no pool, empty, or every token limited or revoked),
    #: or ``refused`` when the pool holds a usable token and refused this start (``refusal`` is its code).
    state: str = Field(pattern=r"^(drawn|none|refused)$")
    detail: str = ""
    #: The pool's refusal code (for one, ``label_lane_cap``); empty unless ``state`` is ``refused``.
    refusal: str = Field(default="", pattern=r"^[a-z_]*$")


class ModelEngineLoginLaneCount(BaseModel):
    """The lanes running under one pool label, as the lane start counted them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(pattern=LABEL_PATTERN)
    lanes: int = Field(ge=0)


class ModelEngineLoginSteer(BaseModel):
    """What a switched-on lane start passes to the pool's ``resolve``: the labels to stay off and the lanes per label.

    Every label matches the pool's label grammar, so the arguments built from it are plain words.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    drain_labels: tuple[Annotated[str, Field(pattern=LABEL_PATTERN)], ...] = ()
    lane_counts: tuple[ModelEngineLoginLaneCount, ...] = ()
    #: Why the drain list is what it is when the verdict could not be had (shown on the start's login line).
    detail: str = ""

    def resolve_arguments(self) -> tuple[str, ...]:
        """The ``resolve`` flags: both always present, so the pool knows a count was made even when it is empty."""
        return (
            "--drain-labels",
            ",".join(self.drain_labels),
            "--lanes",
            ",".join(f"{row.label}={row.lanes}" for row in self.lane_counts),
        )


__all__ = [
    "LABEL_PATTERN",
    "ModelEngineLoginDraw",
    "ModelEngineLoginLaneCount",
    "ModelEngineLoginPool",
    "ModelEngineLoginSteer",
]
