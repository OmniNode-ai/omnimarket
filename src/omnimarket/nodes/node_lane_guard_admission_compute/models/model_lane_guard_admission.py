# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and verdict for the lane guard admission node."""

from pydantic import BaseModel, ConfigDict


class ModelLaneGuardAdmissionRequest(BaseModel):
    """One host's advertised tools and the guard the launcher ships.

    ``tools`` is the capacity advertisement's tools list, whose ``lane-guard:<sha256>`` entries name the installed, hash-verified
    guards; ``expected_sha`` is ``lane_guard.host_sha256()`` of the launcher; ``bus`` is False for a unit that ships the guard inline.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: tuple[str, ...]
    expected_sha: str
    bus: bool = True


class ModelLaneGuardAdmissionVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    installed: tuple[str, ...]
    refusal: str | None
