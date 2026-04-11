# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared ProtocolOverseerVerifier — decouples orchestrators from node internals.

Any node that wants to call the overseer verifier should depend on this
protocol rather than importing HandlerOverseerVerifier directly. This allows
the verifier to be swapped (e.g. for testing) without touching orchestrator code.

Related:
    - OMN-8025: Overseer seam integration epic
    - OMN-8165: Wire overseer verifier into build loop (Phase 1 advisory seam)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from onex_change_control.overseer.model_context_bundle import ModelContextBundle
from onex_change_control.overseer.model_verifier_output import ModelVerifierOutput


@runtime_checkable
class ProtocolOverseerVerifier(Protocol):
    """Protocol for the deterministic overseer verification gate.

    Implementations must be synchronous (not async) — the verifier is a
    pure Python 5-check gate with no I/O. The orchestrator calls it inline
    without awaiting.

    Phase 1 (OMN-8165): advisory only — ESCALATE verdict is logged but does
    not block phase progression.
    Phase 2 (future): hard gate — ESCALATE verdict halts the cycle.
    """

    def verify_with_context(
        self,
        *,
        context: ModelContextBundle,
        domain: str,
        node_id: str,
    ) -> ModelVerifierOutput: ...


__all__: list[str] = ["ProtocolOverseerVerifier"]
