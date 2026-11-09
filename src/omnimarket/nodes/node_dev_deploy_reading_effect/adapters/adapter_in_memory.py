# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory fact source: the default profile, and the one every test breaks on purpose."""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)


class InMemoryDevDeployFacts:
    """Serves the facts it was given; ``facts`` is replaced to break a source."""

    def __init__(self, facts: ModelDevDeployFacts) -> None:
        self.facts = facts

    def read(self, now: datetime) -> ModelDevDeployFacts:
        return self.facts


__all__ = ["InMemoryDevDeployFacts"]
