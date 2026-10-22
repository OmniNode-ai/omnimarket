# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the attention-only push gate (OMN-20896)."""

from __future__ import annotations

from dataclasses import dataclass

from .enum_access_mode import EnumAccessMode


@dataclass(frozen=True, slots=True)
class ModelPushGateRequest:
    """One push. ``idle_seconds`` is the time since the operator's last prompt; None is unknown."""

    message: str
    mode: EnumAccessMode
    idle_seconds: float | None = None
