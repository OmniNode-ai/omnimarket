# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``node_lab_fill_selection_compute`` overlay document (OMN-23312).

The deployment's repository roles and its delegation epics live in
``<overlay root>/node_lab_fill_selection_compute/overlay.yaml``; this model validates that file
and builds the :class:`ModelLabFillDeployment` the selection reads. It carries no value.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lab_fill.model_lab_fill_deployment import ModelLabFillDeployment

OVERLAY_ID = "node_lab_fill_selection_compute"


class ModelLabFillSelectionOverlay(BaseModel):
    """The overlay's keys: the three repository roles, the delegation epics, the optional owners."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    companion_repositories: list[str]
    not_work_repositories: list[str]
    document_repositories: list[str]
    delegation_epics: list[str] = []
    delegation_owners: list[str] = []
    # OMN-19998: the terminal outcomes that hold a ticket out of the fill, and the window in hours
    # the hold lasts; both optional, left out they hold nothing off.
    terminal_hold_outcomes: list[str] = []
    terminal_hold_window_hours: int = Field(default=0, ge=0)

    @classmethod
    def load_deployment(
        cls, environ: Mapping[str, str] | None = None
    ) -> ModelLabFillDeployment:
        """The deployment the first overlay root holding the node's overlay declares; none refuses by name."""
        return load_structured_overlay(OVERLAY_ID, cls, environ).deployment()

    def deployment(self) -> ModelLabFillDeployment:
        """The deployment this document declares; owners left out mean every assignee owns."""
        return ModelLabFillDeployment.from_overlay(self.model_dump())


__all__ = ["OVERLAY_ID", "ModelLabFillSelectionOverlay"]
