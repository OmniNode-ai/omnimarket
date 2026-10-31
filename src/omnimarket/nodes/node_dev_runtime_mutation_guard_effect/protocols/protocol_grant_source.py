# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the guard reads live grants from (OMN-21795)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_dev_runtime_mutation_guard_effect.models.model_dev_runtime_guard_policy import (
    ModelDevRuntimeGrantPolicy,
)


class ProtocolGrantSource(Protocol):
    """Answers which protected projects a live grant row names at a moment."""

    def live_grants(
        self, policy: ModelDevRuntimeGrantPolicy, now: datetime
    ) -> frozenset[str]:
        """The project names some live grant row names; empty when none is readable."""
        ...

    def describe(self, policy: ModelDevRuntimeGrantPolicy) -> str:
        """Where the grants were read from, for the refusal text."""
        ...
