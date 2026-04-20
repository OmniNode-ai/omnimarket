# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Probe interface for node_full_triage_orchestrator.

Every sub-probe (sweep adapter, Infisical, LLM endpoint, launchd tick)
implements this interface. Sub-tickets OMN-9324..9327 add concrete impls.

Contract:
- A probe's `run()` must return a ModelTriageProbeResult — never raise on routine
  failure (network, missing file). Exceptions are caught by the orchestrator
  and converted into an ERROR-status result; probes SHOULD handle their own
  errors to produce richer error_message content.
- Probes MUST respect the `timeout_s` hint. The orchestrator enforces a hard
  timeout independently; the hint is advisory.
- Probes MUST NOT mutate state: no Linear tickets, no PRs, no file writes
  outside `.onex_state/evidence/`, no DB writes.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnibase_core.models.triage import ModelTriageProbeResult


@runtime_checkable
class Probe(Protocol):
    """Structural interface every triage probe must satisfy."""

    probe_name: str

    def run(self, timeout_s: float) -> ModelTriageProbeResult:
        """Execute the probe and return its result. Must not raise on routine failure."""
        ...
