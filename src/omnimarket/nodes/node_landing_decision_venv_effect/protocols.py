# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected subprocess boundary of the landing decision venv effect."""

from collections.abc import Sequence
from typing import Protocol

from omnimarket.nodes.node_landing_decision_venv_effect.models.model_landing_decision_venv import (
    ModelCommandOutcome,
)


class ProtocolCommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], timeout_seconds: float
    ) -> ModelCommandOutcome: ...
