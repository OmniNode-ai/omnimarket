# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the runtime inventory check."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelRuntimeInventoryRequest(BaseModel):
    """Where to find the inventory overlay. Every field is optional; none has a default location.

    Resolution order: ``inventory_path``, then the ``ONEX_WORK_LEDGER_RUNTIME_INVENTORY_FILE``
    environment variable, then ``<root>/work_ledger_runtime_inventory/inventory.yaml`` under each
    entry of ``overlay_roots`` (or, when empty, of the ``ONEX_SKILL_OVERLAY_ROOTS`` variable).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inventory_path: str | None = None
    snapshot_path: str | None = None
    overlay_roots: list[str] = Field(default_factory=list)


class ModelRuntimeInventoryResult(BaseModel):
    """PASS only when every check held; ``findings`` names each refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["PASS", "FAIL"]
    row_count: int = 0
    exclusion_count: int = 0
    observed_count: int = 0
    inventory_path: str = ""
    snapshot_path: str = ""
    findings: list[str] = Field(default_factory=list)
