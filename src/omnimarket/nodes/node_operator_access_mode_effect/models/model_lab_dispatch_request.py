# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input and pool capacity of the lab dispatch gate (OMN-20894)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LabPool:
    """Free slots on eligible lab hosts; None means capacity could not be read."""

    free_slots: int | None
    detail: str


@dataclass(frozen=True, slots=True)
class ModelLabDispatchRequest:
    """One Workflow or Agent call and the pool capacity gathered by the caller."""

    tool_name: str
    tool_input: Mapping[str, Any]
    pool: LabPool
