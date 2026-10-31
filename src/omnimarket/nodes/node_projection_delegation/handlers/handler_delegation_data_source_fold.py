# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure provenance fold for delegate-skill data_source (OMN-19448)."""

from __future__ import annotations

from omnibase_core.enums.enum_delegation_traffic_class import EnumDelegationTrafficClass

from omnimarket.nodes.node_projection_delegation.models.model_data_source_fold_request import (
    ModelDataSourceFoldRequest,
)
from omnimarket.projection.envelope import DATA_SOURCE_EXPERIMENT, DATA_SOURCE_FIXTURE


class HandlerDelegationDataSourceFold:
    """Classify typed synthetic traffic while preserving the seed marker."""

    def handle(self, request: ModelDataSourceFoldRequest) -> str:
        if request.envelope_source == DATA_SOURCE_FIXTURE:
            return DATA_SOURCE_FIXTURE
        provenance = request.terminal.provenance
        if (
            provenance is not None
            and provenance.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
        ):
            return DATA_SOURCE_EXPERIMENT
        return request.envelope_source


__all__ = ["HandlerDelegationDataSourceFold", "ModelDataSourceFoldRequest"]
