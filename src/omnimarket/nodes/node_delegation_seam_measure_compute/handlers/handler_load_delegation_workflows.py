# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``load_delegation_workflows``: return the typed rows of the shipped registry.

Canonical definition-B shape: ``handle(ModelLoadDelegationWorkflowsRequest) ->
ModelLoadDelegationWorkflowsResult``. The registry is the data file shipped with
the node; the node takes ``now`` as a fact, reads no clock and uses no protocol.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.contract_topics import contract_section
from omnimarket.nodes.node_delegation_seam_measure_compute.models.model_delegation_workflow import (
    ModelDelegationWorkflowRegistry,
    ModelLoadDelegationWorkflowsRequest,
    ModelLoadDelegationWorkflowsResult,
)

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "delegation_workflows.yaml"


def load_registry(path: Path = REGISTRY_PATH) -> ModelDelegationWorkflowRegistry:
    """Parse and validate a registry file; fail closed on a missing or empty cell."""
    raw = {
        "station_shapes": contract_section(path, "station_shapes"),
        "workflows": contract_section(path, "workflows"),
    }
    return ModelDelegationWorkflowRegistry.model_validate(raw).check_complete()


class HandlerLoadDelegationWorkflows:
    """COMPUTE: the typed delegation workflow rows."""

    def __init__(self, registry_path: Path = REGISTRY_PATH) -> None:
        self._registry_path = registry_path

    def handle(
        self, request: ModelLoadDelegationWorkflowsRequest
    ) -> ModelLoadDelegationWorkflowsResult:
        registry = load_registry(self._registry_path)
        wanted = set(request.workflow_ids)
        known = {workflow.id for workflow in registry.workflows}
        if unknown := sorted(wanted - known):
            raise ValueError(f"unknown workflow ids: {unknown}")
        rows = tuple(
            workflow
            for workflow in registry.workflows
            if not wanted or workflow.id in wanted
        )
        return ModelLoadDelegationWorkflowsResult(
            read_at=request.now,
            station_shapes=registry.station_shapes,
            workflows=rows,
        )
