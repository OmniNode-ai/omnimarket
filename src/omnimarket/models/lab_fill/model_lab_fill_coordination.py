# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``coordination`` key of the selection overlay: dependencies, the shared claim lock, the done gate (OMN-26762).

Each block is optional; a block left out turns its rule off. Inside a block every key is required.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelLabFillDependencyRule(BaseModel):
    """Whether a ticket with an open blocker in Linear is held out of the fill."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skip_blocked: bool


class ModelLabFillClaimLockConfig(BaseModel):
    """The shared lock adapter that serializes placements, and how long one claim holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str = Field(min_length=1)
    ttl_seconds: int = Field(ge=1)
    # The moved ledger reader the ``ledger`` adapter reads claims through; the other adapters leave it empty.
    reader: str = ""
    # The environment variable naming the ledger the ``ledger`` adapter appends through; the overlay names it.
    ledger_path_env: str = ""


class ModelLabFillDoneGate(BaseModel):
    """Evidence items a local done-gate receipt must carry as VERIFIED before a ticket moves to Done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_items: tuple[str, ...] = Field(min_length=1)
    done_statuses: tuple[str, ...] = ("Done",)


class ModelLabFillCoordination(BaseModel):
    """The overlay's coordination rules."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dependencies: ModelLabFillDependencyRule | None = None
    claim_lock: ModelLabFillClaimLockConfig | None = None
    done_gate: ModelLabFillDoneGate | None = None


__all__ = [
    "ModelLabFillClaimLockConfig",
    "ModelLabFillCoordination",
    "ModelLabFillDependencyRule",
    "ModelLabFillDoneGate",
]
