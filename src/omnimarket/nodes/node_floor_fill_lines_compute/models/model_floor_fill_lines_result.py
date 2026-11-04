# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lines a tick prints, in the tick's finding shape."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_floor_fill_lines_compute.models.enum_floor_fill_lines_level import (
    EnumFloorFillLinesLevel,
)


class ModelFloorFillLinesFinding(BaseModel):
    """One finding: ``(level, loop, why, fix)`` is the tuple the tick's findings use.

    ``loop`` is the key cell, such as ``line-stopped:<line_id>``; ``why`` is the text after
    the bar (empty for a finding that prints nothing after its key); ``fix`` is what clears
    a MISSING or UNKNOWN line and is empty for a NOTE.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: EnumFloorFillLinesLevel
    loop: str
    why: str = ""
    fix: str = ""

    def as_tuple(self) -> tuple[str, str, str, str]:
        """The tick's finding tuple."""
        return (self.level.value, self.loop, self.why, self.fix)

    def render(self) -> str:
        """The printed line, ``MISSING <loop> | <why>`` (no bar when ``why`` is empty)."""
        head = f"{self.level.value.upper()} {self.loop}"
        return f"{head} | {self.why}" if self.why else head


class ModelFloorFillLinesResult(BaseModel):
    """The findings of one pass, in the order they print."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelFloorFillLinesFinding, ...] = ()

    def rendered(self) -> tuple[str, ...]:
        """Every printed line, in order."""
        return tuple(finding.render() for finding in self.findings)


__all__: list[str] = ["ModelFloorFillLinesFinding", "ModelFloorFillLinesResult"]
