# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer every supervision check gives (OMN-20893, OMN-20894, OMN-20896)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ModelHookVerdict:
    """``allowed`` False refuses the action; ``reason`` goes back to the model verbatim.

    A refusal also names the gate that refused (``guard``) and its reason class
    (``reason_class``), the two cells of its refusal row (OMN-21020). Both are
    left out of equality: what a gate allows or refuses is ``allowed`` and ``reason``.
    """

    allowed: bool
    reason: str = ""
    guard: str = field(default="", compare=False)
    reason_class: str = field(default="", compare=False)

    @classmethod
    def allow(cls) -> ModelHookVerdict:
        return cls(True, "")

    @classmethod
    def refuse(
        cls, reason: str, guard: str = "", reason_class: str = ""
    ) -> ModelHookVerdict:
        return cls(False, reason, guard, reason_class)
