# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_review_gate_context_orchestrator: every sweep setting, none in code.

Found at ``<root>/node_review_gate_context_orchestrator/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it. The review-check POLICY
(check name, bypass classes, titles) is not here: it is read from the compute node's overlay
(``omnimarket.nodes.node_review_gate_context_compute.overlay.load_overlay``), so the policy has
one home.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)

NODE_NAME = "node_review_gate_context_orchestrator"


class ModelReviewGateSweepSettings(BaseModel):
    """The sweep settings: which repositories, how fresh the watcher state must be, and which
    ledger lanes and engines record an accepted review verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner: str = Field(min_length=1)
    #: The bare repository names the sweep covers when the command names none.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: The NAME of the environment variable that holds the PR watcher's state file path; the code
    #: reads the path from that variable, never a path from the overlay.
    watcher_state_path_env: str = Field(min_length=1)
    #: How old the watcher state's ``last_tick`` may be; older, the sweep reads nothing.
    max_age_seconds: int = Field(ge=1)
    include_drafts: bool
    #: How far back the ledger is read for verdict rows.
    lookback_days: float = Field(gt=0)
    #: A verdict row counts only when its lane starts with one of these.
    review_lane_prefixes: tuple[str, ...] = Field(min_length=1)
    #: A verdict row counts only when its ``review=`` cell names one of these.
    review_engines: tuple[str, ...] = Field(min_length=1)


class ModelReviewGateSweepSchedule(BaseModel):
    """The unattended invocation is a read-only probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["probe"]


class ModelReviewGateSweepOverlay(BaseModel):
    """The sources and optional scheduled trigger of the review-gate sweep."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: ModelReviewGateSweepSettings
    ledger_read: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute = Field(
        discriminator="route"
    )
    schedule: ModelReviewGateSweepSchedule | None = None


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelReviewGateSweepOverlay:
    return load_structured_overlay(NODE_NAME, ModelReviewGateSweepOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelReviewGateSweepOverlay",
    "ModelReviewGateSweepSettings",
    "load_overlay",
]
