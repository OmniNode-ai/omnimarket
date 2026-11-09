# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``node_lab_fill_selection_compute`` overlay document (OMN-23312).

The deployment's repository roles and its delegation epics live in
``<overlay root>/node_lab_fill_selection_compute/overlay.yaml``; this model validates that file
and builds the :class:`ModelLabFillDeployment` the selection reads. It carries no value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_lab_fill_deployment import ModelLabFillDeployment


class ModelLabFillSelectionOverlay(BaseModel):
    """The overlay's keys: the three repository roles, the delegation epics, the optional owners."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    companion_repositories: list[str]
    not_work_repositories: list[str]
    document_repositories: list[str]
    delegation_epics: list[str] = []
    delegation_owners: list[str] = []

    def deployment(self) -> ModelLabFillDeployment:
        """The deployment this document declares; owners left out mean every assignee owns."""
        return ModelLabFillDeployment.from_overlay(self.model_dump())


__all__ = ["ModelLabFillSelectionOverlay"]
