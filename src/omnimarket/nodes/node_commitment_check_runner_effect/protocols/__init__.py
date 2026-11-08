# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runner's one new port: how a check's argv is executed. Reading and appending ledger rows use the reading writer's."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
)


class ProtocolCheckExecutor(Protocol):
    """Run one check argv with no shell and report how it ended."""

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCheckExecution: ...


__all__ = ["ProtocolCheckExecutor"]
