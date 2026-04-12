# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Placeholder protocol implementations for HandlerBuildLoopExecutor DI skeleton (OMN-8450).

These placeholders satisfy the Protocol interfaces and are used as defaults in
_ensure_sub_handlers() until real implementations are wired in Wave 3.
Each returns a PhaseResult(success=False, side_effect_summary="skipped: not yet wired").
Real implementations are injected via HandlerBuildLoopExecutor constructor kwargs.
"""

from __future__ import annotations

from omnimarket.nodes.node_overnight.protocols.protocol_phase_handlers import (
    PhaseResult,
)

_NOT_WIRED = "skipped: protocol slot not yet wired (Wave 3)"


class _PlaceholderNightlyLoop:
    """Placeholder for ProtocolNightlyLoopHandler — wire HandlerNightlyLoopController in Wave 3."""

    def handle(self, *, correlation_id: str, dry_run: bool = False) -> PhaseResult:
        return PhaseResult(
            success=False,
            side_effect_summary=_NOT_WIRED,
            duration_seconds=0.0,
            error_message="nightly_loop not wired",
        )


class _PlaceholderBuildLoop:
    """Placeholder for ProtocolBuildLoopPhaseHandler — wire HandlerBuildLoopOrchestrator in Wave 3."""

    def handle(
        self, *, correlation_id: str, max_cycles: int, dry_run: bool = False
    ) -> PhaseResult:
        return PhaseResult(
            success=False,
            side_effect_summary=_NOT_WIRED,
            duration_seconds=0.0,
            error_message="build_loop not wired",
        )


class _PlaceholderMergeSweep:
    """Placeholder for ProtocolMergeSweepHandler — wire NodeMergeSweep adapter in Wave 3."""

    def handle(
        self,
        *,
        correlation_id: str,
        pr_inventory: tuple[str, ...],
        dry_run: bool = False,
    ) -> PhaseResult:
        return PhaseResult(
            success=False,
            side_effect_summary=_NOT_WIRED,
            duration_seconds=0.0,
            error_message="merge_sweep not wired",
        )


class _PlaceholderCiWatch:
    """Placeholder for ProtocolCiWatchHandler — wire HandlerCiWatchClient in Wave 3."""

    def handle(
        self,
        *,
        correlation_id: str,
        pr_refs: tuple[str, ...],
        timeout_seconds: int = 300,
    ) -> PhaseResult:
        return PhaseResult(
            success=False,
            side_effect_summary=_NOT_WIRED,
            duration_seconds=0.0,
            error_message="ci_watch not wired",
        )


class _PlaceholderPlatformReadiness:
    """Placeholder for ProtocolPlatformReadinessHandler — wire NodePlatformReadiness in Wave 3."""

    def handle(self, *, correlation_id: str, dry_run: bool = False) -> PhaseResult:
        return PhaseResult(
            success=False,
            side_effect_summary=_NOT_WIRED,
            duration_seconds=0.0,
            error_message="platform_readiness not wired",
        )


__all__ = [
    "_PlaceholderBuildLoop",
    "_PlaceholderCiWatch",
    "_PlaceholderMergeSweep",
    "_PlaceholderNightlyLoop",
    "_PlaceholderPlatformReadiness",
]
