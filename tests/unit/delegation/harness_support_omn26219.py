# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures of the harness backend handler tests (OMN-26219)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import yaml

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.wire.model_bifrost_delegation_config import (
    EnumDelegationBackendSurface,
    ModelDelegationBackendConfig,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_harness_backend_call import (
    HarnessExitCause,
    ModelHarnessAttemptRecord,
    ModelHarnessBackendCallRequest,
    ModelHarnessGateVerdict,
    ModelHarnessRunReceipt,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "harness_handlers"
OVERLAY_FIXTURE = FIXTURES / "delegation_routing.harness.overlay.yaml"
RECORDED_ROW = FIXTURES / "dev_row_glm_flash_code_generation_provider_error.json"


def load_overlay() -> ModelDelegationRoutingOverlay:
    return ModelDelegationRoutingOverlay.model_validate(
        yaml.safe_load(OVERLAY_FIXTURE.read_text())
    )


def recorded_row() -> dict[str, Any]:
    return json.loads(RECORDED_ROW.read_text())


def call_request(
    task_class: str = "code_generation",
    *,
    tenant_slug: str | None = HOUSE_TENANT_SLUG,
    surface: EnumDelegationBackendSurface = EnumDelegationBackendSurface.INTERNAL,
    pinned_backend_id: str | None = None,
    request_id: UUID | None = None,
) -> ModelHarnessBackendCallRequest:
    return ModelHarnessBackendCallRequest(
        request_id=request_id or uuid4(),
        task_class=task_class,
        prompt="Write a Python function add(a, b) that returns the sum of a and b.",
        tenant_slug=tenant_slug,
        surface=surface,
        pinned_backend_id=pinned_backend_id,
        timeout_seconds=60,
        caller_lane="harnessA-1605",
    )


def receipt(
    answer: str = "",
    *,
    cause: HarnessExitCause = "completed",
    wall: float = 1.5,
    cost: Decimal | None = None,
) -> ModelHarnessRunReceipt:
    return ModelHarnessRunReceipt(
        answer=answer,
        tokens_in=0 if cause != "completed" else 40,
        tokens_out=0 if cause != "completed" else 30,
        cost_usd=cost,
        wall_seconds=wall,
        exit_cause=cause,
    )


class ScriptedRunner:
    """Replays one receipt per call, in order, and records which backends ran."""

    def __init__(self, receipts: Sequence[ModelHarnessRunReceipt]) -> None:
        self._receipts = list(receipts)
        self.ran: list[str] = []

    async def run(
        self,
        backend: ModelDelegationBackendConfig,
        prompt: str,
        timeout_seconds: float,
    ) -> ModelHarnessRunReceipt:
        self.ran.append(backend.backend_id)
        return self._receipts[len(self.ran) - 1]


class AcceptAllGate:
    def judge(
        self,
        request: ModelHarnessBackendCallRequest,
        backend: ModelDelegationBackendConfig,
        answer: str,
    ) -> ModelHarnessGateVerdict:
        return ModelHarnessGateVerdict(accepted=True, gate_kind="contract_only")


class ListCostSink:
    def __init__(self) -> None:
        self.records: list[ModelHarnessAttemptRecord] = []

    def record(self, attempt: ModelHarnessAttemptRecord) -> None:
        self.records.append(attempt)


class TierSettings:
    """The settings store seam, injected: the ``claude_harness_tiers`` document, one row per backend id."""

    def __init__(self, tiers: dict[str, dict[str, Any]] | None = None) -> None:
        self._tiers = tiers

    def read(self, name: str) -> dict[str, Any] | None:
        if name != "claude_harness_tiers" or self._tiers is None:
            return None
        return {"schema_version": 1, "tiers": self._tiers}


def tier_row(order: list[str], effort: str | None = None) -> dict[str, Any]:
    return {"credential_order": order, **({"effort": effort} if effort else {})}
