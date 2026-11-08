# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect-backend report shared by the two-backend suite and the reading writer (OMN-22911).

Two nodes read this report, so it is owned here and not by either node: ``ModelEffectBackendReport`` is the core
effect-backends validator's report as JSON, and ``ModelReportRead`` is the report or the reason it could not be read.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelEffectBackendEntry(BaseModel):
    """One effect node as the core validator reports it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    node_name: str
    path: str = ""
    has_in_memory_default: bool = False
    has_real_backend_overlay: bool = False
    in_memory_factory: str = ""
    real_factory: str = ""
    overlay_key: str = ""
    scenario: str = ""

    @property
    def on_both(self) -> bool:
        return self.has_in_memory_default and self.has_real_backend_overlay


class ModelEffectBackendReport(BaseModel):
    """The core validator's report; the counts are derived from the entries, never read back."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # Required: a report with no ``entries`` key is a missing enumeration, never an empty inventory. An explicitly empty
    # inventory is ``entries: []``.
    entries: tuple[ModelEffectBackendEntry, ...]
    unreadable: tuple[str, ...] = ()

    @property
    def incomplete_cause(self) -> str:
        """Why the inventory cannot stand as the whole list of effects, or an empty string when it is complete."""
        if self.unreadable:
            return f"report-incomplete:{len(self.unreadable)}-unreadable"
        return ""

    @property
    def not_on_both_count(self) -> int:
        return sum(1 for e in self.entries if not e.on_both)


class ModelReportRead(BaseModel):
    """The report, or the reason it could not be read. An error is never an empty list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    report: ModelEffectBackendReport | None = None
    error: str | None = None


__all__ = ["ModelEffectBackendEntry", "ModelEffectBackendReport", "ModelReportRead"]
