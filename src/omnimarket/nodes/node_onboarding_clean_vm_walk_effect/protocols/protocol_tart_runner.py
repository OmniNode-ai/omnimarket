# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport boundary for guarded Tart commands."""

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.models.model_clean_vm_walk import (
    ModelRunnerResult,
)


class ProtocolTartRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ModelRunnerResult:
        """Run a foreground command and collect its output."""
        ...

    def start(self, argv: Sequence[str]) -> None:
        """Detach the single tart run command without changing its guarded argv."""
        ...
