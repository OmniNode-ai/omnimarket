# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase handler protocols and PhaseResult model for HandlerBuildLoopExecutor.

Defines the 5 injectable protocol slots and the vacuous-green-proof PhaseResult
model. Every protocol implementation must return a PhaseResult with a non-empty
side_effect_summary when success=True — the Pydantic validator enforces this at
construction time (OMN-8449).
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, field_validator
from typing_extensions import runtime_checkable


class PhaseResult(BaseModel):
    """Result returned by every phase handler protocol.

    Vacuous-green prevention: side_effect_summary must be non-empty when
    success=True. Use 'skipped: <reason>' for explicit skip results.
    duration_seconds must be > 0.1 for any real I/O phase.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    side_effect_summary: str
    duration_seconds: float
    error_message: str | None = None

    @field_validator("side_effect_summary")
    @classmethod
    def summary_not_empty_on_success(cls, v: str, info: Any) -> str:
        if not v and info.data.get("success", False):
            raise ValueError(
                "side_effect_summary must be non-empty when success=True. "
                "Use 'skipped: <reason>' for explicit skip results."
            )
        return v


@runtime_checkable
class ProtocolNightlyLoopHandler(Protocol):
    """Wraps HandlerNightlyLoopController — reads standing orders, dispatches tickets."""

    def handle(
        self,
        *,
        correlation_id: str,
        dry_run: bool = False,
    ) -> PhaseResult: ...


@runtime_checkable
class ProtocolBuildLoopPhaseHandler(Protocol):
    """Wraps HandlerBuildLoopOrchestrator — inner cycle: fill/classify/build/verify."""

    def handle(
        self,
        *,
        correlation_id: str,
        max_cycles: int,
        dry_run: bool = False,
    ) -> PhaseResult: ...


@runtime_checkable
class ProtocolMergeSweepHandler(Protocol):
    """Wraps NodeMergeSweep — reviews and merges PRs from inventory.

    pr_inventory=() MUST return PhaseResult(success=True, side_effect_summary='skipped: no PR inventory').
    """

    def handle(
        self,
        *,
        correlation_id: str,
        pr_inventory: tuple[str, ...],
        dry_run: bool = False,
    ) -> PhaseResult: ...


@runtime_checkable
class ProtocolCiWatchHandler(Protocol):
    """Watches CI status for PR refs via gh run view.

    pr_refs=() MUST return PhaseResult(success=True, side_effect_summary='skipped: no PR refs').
    """

    def handle(
        self,
        *,
        correlation_id: str,
        pr_refs: tuple[str, ...],
        timeout_seconds: int = 300,
    ) -> PhaseResult: ...


@runtime_checkable
class ProtocolPlatformReadinessHandler(Protocol):
    """Wraps NodePlatformReadiness — checks platform health dimensions."""

    def handle(
        self,
        *,
        correlation_id: str,
        dry_run: bool = False,
    ) -> PhaseResult: ...


__all__ = [
    "PhaseResult",
    "ProtocolBuildLoopPhaseHandler",
    "ProtocolCiWatchHandler",
    "ProtocolMergeSweepHandler",
    "ProtocolNightlyLoopHandler",
    "ProtocolPlatformReadinessHandler",
]
