# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime probe protocol of node_branch_deploy_probe_effect."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact


class ProtocolBranchDeployProbe(Protocol):
    """The runtime probe port the handler takes as a constructor argument.

    Satisfied by ``HttpRuntimeProbe`` of this node's HTTP adapter and by
    ``InMemoryBranchDeployProbe`` of its in-memory adapter.
    """

    def read(
        self, now: datetime, expected_sha: str
    ) -> tuple[tuple[ModelRuntimeProbeFact, ...], str | None]: ...
