# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output of the access-mode detector (OMN-20892)."""

from __future__ import annotations

from dataclasses import dataclass

from .enum_access_mode import EnumAccessMode


@dataclass(frozen=True, slots=True)
class ModelAccessModeResult:
    """The mode the rules use, where it came from, and what detection alone said.

    ``source`` is one of ``env-override`` (OMNI_ACCESS_MODE), ``session-override`` (the prompt
    command), ``global-override`` (onex-access-mode set), ``detected`` or ``unknown`` (no signal,
    resolved to REMOTE). ``signals`` names the environment markers detection read.
    """

    mode: EnumAccessMode
    source: str
    detected: EnumAccessMode | None
    signals: tuple[str, ...] = ()

    @property
    def operator_facing(self) -> bool:
        return self.mode is not EnumAccessMode.HEADLESS

    def describe(self) -> str:
        why = ", ".join(self.signals) if self.signals else "no client signal"
        return f"{self.mode.value} ({self.source}: {why})"
