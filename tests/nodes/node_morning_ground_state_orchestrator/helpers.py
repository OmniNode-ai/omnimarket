# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the public test overlay, a recorded phase gateway, a request builder."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    OVERLAY_ENV,
    load_morning_overlay,
)
from omnimarket.nodes.node_morning_ground_state_orchestrator.models import (
    ModelMorningGroundStateRequest,
    ModelMorningOverlay,
    ModelMorningPhaseRequest,
)

NODE = "node_morning_ground_state_orchestrator"
OVERLAY_ROOT = Path(__file__).parent / "fixtures" / "overlay"
OVERLAY_FILE = OVERLAY_ROOT / NODE / "overlay.yaml"
PHASES = ["GroundState", "Triage", "Reconcile", "Integrate", "DroppedWork", "Goal"]


def public_overlay() -> ModelMorningOverlay:
    """Load the public test overlay through the production loader."""
    import os

    previous = os.environ.get(OVERLAY_ENV)
    os.environ[OVERLAY_ENV] = str(OVERLAY_FILE)
    try:
        return load_morning_overlay()
    finally:
        if previous is None:
            del os.environ[OVERLAY_ENV]
        else:
            os.environ[OVERLAY_ENV] = previous


class FixtureGateway:
    """Recorded agent results; exercises orchestration without any agent."""

    def __init__(self, precheck: dict[str, JsonValue] | None = None) -> None:
        self.precheck = precheck
        self.calls: list[dict[str, JsonValue]] = []
        self.prompts: dict[str, str] = {}
        self.reconciled: list[bool] = []
        self.fail_reconcile = False
        self.fail_precheck = False

    async def reconcile(self, run: ModelMorningGroundStateRequest) -> None:
        self.reconciled.append(run.publish)
        if self.fail_reconcile:
            raise RuntimeError("unavailable reconciler")

    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None:
        self.prompts[phase.label] = phase.prompt
        self.calls.append(
            {
                "label": phase.label,
                "phase": phase.phase,
                "model": phase.model,
                "effort": phase.effort,
                "schema": phase.schema_definition,
            }
        )
        if phase.label == "idempotency-precheck":
            if self.fail_precheck:
                raise TimeoutError("dead precheck agent")
            return self.precheck
        return {
            "state": "stub",
            "detail": "fixture",
            "prs": [],
            "residuals": [],
            "delegation": {"delegated": 1, "runs": ["fixture"], "reason": ""},
        }


class PhaseEvidenceGateway(FixtureGateway):
    """Replaces one phase's delegation cell."""

    def __init__(self, phase: str, cell: JsonValue) -> None:
        super().__init__()
        self.phase = phase
        self.cell = cell

    async def run(
        self, run: ModelMorningGroundStateRequest, phase: ModelMorningPhaseRequest
    ) -> dict[str, JsonValue] | None:
        result = await super().run(run, phase)
        if phase.phase == self.phase and result is not None:
            result["delegation"] = self.cell
        return result


def request(args: dict[str, JsonValue] | None = None) -> ModelMorningGroundStateRequest:
    return ModelMorningGroundStateRequest.model_validate(
        {
            "date": "2026-08-30",
            "correlation_id": "12345678-1234-5678-1234-567812345678",
            "emitted_at": "2026-10-06T22:19:12Z",
            "tenant_id": "tenant-a",
            **(args or {}),
        }
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
