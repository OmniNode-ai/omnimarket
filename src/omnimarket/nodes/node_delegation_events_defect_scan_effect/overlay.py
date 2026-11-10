# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_delegation_events_defect_scan_effect: where the rows are read and where the row is appended, none in code.

Found at ``<root>/node_delegation_events_defect_scan_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
day, the grace, the row words and the bounds are the compute node's overlay (``node_delegation_events_defect_scan_compute``), loaded
beside this one. Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_delegation_events_defect_scan import ModelDefectScanOverlay
from omnimarket.nodes.node_delegation_completion_rate_effect.overlay import (
    ModelDelegationEventsSource,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_delegation_events_defect_scan_effect"
COMPUTE_NODE_NAME = "node_delegation_events_defect_scan_compute"


class ModelDefectScanPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str = Field(min_length=1)


class ModelDefectScanEffectOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    events_source: ModelDelegationEventsSource
    ledger_append: ModelLedgerAppendRoute
    publish: ModelDefectScanPublish

    def carried_environment_names(self) -> tuple[str, ...]:
        """The variables a scheduled unit must carry for this overlay to resolve: the ssh host and the ledger route."""
        return (
            self.events_source.ssh_host_env,
            self.ledger_append.command_env,
            self.ledger_append.host_env,
        )


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDefectScanEffectOverlay:
    return load_structured_overlay(NODE_NAME, ModelDefectScanEffectOverlay, environ)


def load_scan_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDefectScanOverlay:
    """The compute node's overlay: the day, the grace, the row words and the floor owner's bounds."""
    return load_structured_overlay(COMPUTE_NODE_NAME, ModelDefectScanOverlay, environ)


__all__ = [
    "COMPUTE_NODE_NAME",
    "NODE_NAME",
    "ModelDefectScanEffectOverlay",
    "ModelDefectScanPublish",
    "load_overlay",
    "load_scan_overlay",
]
