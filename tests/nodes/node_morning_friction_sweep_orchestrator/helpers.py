# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the public test overlay, a recorded phase gateway, a request builder."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import JsonValue

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_morning_friction_sweep import (
    OVERLAY_ENV,
    load_friction_overlay,
)
from omnimarket.nodes.node_morning_friction_sweep_orchestrator.models import (
    ModelFrictionOverlay,
    ModelFrictionPhaseRequest,
    ModelMorningFrictionSweepRequest,
)

NODE = "node_morning_friction_sweep_orchestrator"
OVERLAY_ROOT = Path(__file__).parent / "fixtures" / "overlay"
OVERLAY_FILE = OVERLAY_ROOT / NODE / "overlay.yaml"
DELEGATION: dict[str, JsonValue] = {"delegated": 1, "runs": ["fixture"], "reason": ""}


def public_overlay() -> ModelFrictionOverlay:
    """Load the public test overlay through the production loader."""
    previous = os.environ.get(OVERLAY_ENV)
    os.environ[OVERLAY_ENV] = str(OVERLAY_FILE)
    try:
        return load_friction_overlay()
    finally:
        if previous is None:
            del os.environ[OVERLAY_ENV]
        else:
            os.environ[OVERLAY_ENV] = previous


def stub_result(label: str) -> dict[str, JsonValue] | None:
    """A schema-valid recorded phase result carrying a valid delegation cell."""
    if label == "friction-precheck":
        return {
            "date": "2026-08-29",
            "clock_utc": "2026-08-29T15:47:00Z",
            "verdict": "run",
            "evidence": "",
            "reason": "fixture",
        }
    if label == "friction-scan":
        return {
            "handoff_path": "/tmp/scan.json",
            "sources_read": ["a"],
            "candidate_count": 1,
            "unreadable_sources": [],
            "ledger_rows": [],
        }
    if label.startswith("friction-source-"):
        return {
            "handoff_path": f"/tmp/{label}.json",
            "source_id": label.removeprefix("friction-source-"),
            "verdict": "READ",
            "freshness": "fixture",
            "candidate_count": 1,
            "unreadable": [],
            "ledger_rows": [],
        }
    if label == "friction-synthesize":
        return {
            "handoff_path": "/tmp/synthesis.json",
            "candidate_count": 1,
            "root_causes": ["cause"],
            "sources_unknown": [],
            "pickups": ["pickup"],
            "ledger_rows": [],
            "delegation": DELEGATION,
        }
    if label == "friction-adjudicate":
        return {
            "handoff_path": "/tmp/adjudication.json",
            "filed": [],
            "commented": [],
            "known": [],
            "first_occurrence": [],
            "escalated": [],
            "ledger_rows": [],
            "delegation": DELEGATION,
        }
    return {
        "report_path": "/tmp/report.md",
        "state_path": "/tmp/state.json",
        "commit_sha": "deadbeef",
        "watermark_summary": "fixture",
        "ledger_rows": [],
        "premise_audit": {
            "status": "UNKNOWN",
            "rule_landed_at": "",
            "rule_evidence": "",
            "eligible_terminal_rows": 0,
            "falsified_terminal_rows": None,
            "positive_controls": [],
            "unreadable_sources": ["fixture"],
            "reason": "fixture has no ledger",
        },
        "delegation": DELEGATION,
    }


class FixtureGateway:
    """Recorded agent results; exercises orchestration without any agent."""

    def __init__(self, responses: dict[str, JsonValue] | None = None) -> None:
        self.responses = responses
        self.calls: list[dict[str, JsonValue]] = []
        self.prompts: dict[str, str] = {}
        self.fail_precheck = False
        self.active = 0
        self.peak = 0

    async def run(
        self, run: ModelMorningFrictionSweepRequest, phase: ModelFrictionPhaseRequest
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
        if phase.label == "friction-precheck" and self.fail_precheck:
            raise TimeoutError("dead precheck agent")
        if phase.phase == "Scan":
            import asyncio

            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0)
            self.active -= 1
        if self.responses is not None:
            value = self.responses[phase.label]
            return value if isinstance(value, dict) or value is None else None
        return stub_result(phase.label)


class PhaseEvidenceGateway(FixtureGateway):
    """Replaces one phase's delegation cell."""

    def __init__(self, label: str, cell: JsonValue) -> None:
        super().__init__()
        self.label = label
        self.cell = cell

    async def run(
        self, run: ModelMorningFrictionSweepRequest, phase: ModelFrictionPhaseRequest
    ) -> dict[str, JsonValue] | None:
        result = await super().run(run, phase)
        if phase.label == self.label and result is not None:
            result["delegation"] = self.cell
        return result


def request(
    args: dict[str, JsonValue] | None = None,
) -> ModelMorningFrictionSweepRequest:
    return ModelMorningFrictionSweepRequest.model_validate(
        {
            "date": "2026-08-29",
            "correlation_id": "12345678-1234-5678-1234-567812345678",
            "emitted_at": "2026-10-06T22:19:12Z",
            "tenant_id": "tenant-a",
            **(args or {}),
        }
    )
