# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected arm adapter seam; effects and transport are bound by the container."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_compaction_arm_run_effect.handlers.workspace import (
    ModelArmWorkspace,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelArmRunRequest,
    ModelCompactionObservation,
)


class ProtocolArmAdapter(Protocol):
    def compact(
        self, request: ModelArmRunRequest, workspace: ModelArmWorkspace
    ) -> ModelCompactionObservation: ...
