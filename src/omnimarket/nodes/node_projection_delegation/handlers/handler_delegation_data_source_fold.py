# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure provenance fold for delegate-skill data_source (OMN-19448)."""

from __future__ import annotations

from omnibase_core.enums.enum_delegation_traffic_class import EnumDelegationTrafficClass

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.projection.envelope import DATA_SOURCE_EXPERIMENT, DATA_SOURCE_FIXTURE


class HandlerDelegationDataSourceFold:
    """Classify typed synthetic traffic while preserving the seed marker."""

    def handle(
        self, terminal: ModelDelegateSkillTerminalProjection, envelope_source: str
    ) -> str:
        if envelope_source == DATA_SOURCE_FIXTURE:
            return DATA_SOURCE_FIXTURE
        if (
            terminal.provenance is not None
            and terminal.provenance.traffic_class
            is EnumDelegationTrafficClass.SYNTHETIC
        ):
            return DATA_SOURCE_EXPERIMENT
        return envelope_source


__all__ = ["HandlerDelegationDataSourceFold"]
