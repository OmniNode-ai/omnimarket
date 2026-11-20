# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The handler of node_branch_deploy_probe_effect.

``HandlerBranchDeployProbe`` reads the running runtime's build provenance, health and readiness through an injected
probe port and maps what it read to a typed result. When no probe is given it builds the HTTP runtime probe from the
overlay's settings. It reports the observed sha; it does not judge it against the expected one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.nodes.node_branch_deploy_probe_effect.adapters.adapter_http_probe import (
    HttpRuntimeProbe,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.models.model_branch_deploy_probe import (
    ModelBranchDeployProbeRequest,
    ModelBranchDeployProbeResult,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.overlay import (
    ModelBranchDeployProbeOverlay,
    load_overlay,
)
from omnimarket.nodes.node_branch_deploy_probe_effect.protocols.protocol_branch_deploy_probe import (
    ProtocolBranchDeployProbe,
)


class HandlerBranchDeployProbe:
    """Reads the running runtime's build provenance and health for a branch deploy."""

    def __init__(
        self,
        overlay: ModelBranchDeployProbeOverlay | None = None,
        probe: ProtocolBranchDeployProbe | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._configured_overlay = overlay
        self._configured_probe = probe
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    @property
    def _overlay(self) -> ModelBranchDeployProbeOverlay:
        if self._configured_overlay is None:
            self._configured_overlay = load_overlay()
        return self._configured_overlay

    @property
    def _probe(self) -> ProtocolBranchDeployProbe:
        if self._configured_probe is None:
            self._configured_probe = HttpRuntimeProbe(self._overlay.probe)
        return self._configured_probe

    def handle(
        self, request: ModelBranchDeployProbeRequest
    ) -> ModelBranchDeployProbeResult:
        facts, error = self._probe.read(self._clock(), request.expected_sha)
        if error is not None or not facts:
            detail = (error or "")[: self._overlay.detail_max_chars]
            return ModelBranchDeployProbeResult(
                correlation_id=request.correlation_id,
                phase=request.phase,
                status="unreadable",
                observed_sha="",
                healthy=False,
                expected_sha=request.expected_sha,
                detail=detail,
            )
        fact = facts[0]
        return ModelBranchDeployProbeResult(
            correlation_id=request.correlation_id,
            phase=request.phase,
            status="read",
            observed_sha=fact.observed_sha or "",
            healthy=fact.ok,
            expected_sha=request.expected_sha,
            detail="",
        )
