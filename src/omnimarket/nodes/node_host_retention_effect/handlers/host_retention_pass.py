# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The accumulator one host retention pass fills, shared by the handler and the uv cap pass."""

from __future__ import annotations

from dataclasses import dataclass, field

from omnimarket.nodes.node_host_retention_effect.models.model_host_retention import (
    ModelRetentionItem,
    ModelRetentionKept,
)


@dataclass
class _Pass:
    """What one pass has found so far; every list is exact, nothing is capped."""

    items: list[ModelRetentionItem] = field(default_factory=list)
    kept: list[ModelRetentionKept] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed: dict[str, int] = field(default_factory=dict)
    freed: dict[str, int] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)

    def keep(self, class_name: str, path: str, reason: str) -> None:
        self.kept.append(
            ModelRetentionKept(class_name=class_name, path=path, reason=reason)
        )

    def propose(self, item: ModelRetentionItem) -> None:
        self.items.append(item)

    def done(self, class_name: str, freed: int) -> None:
        self.removed[class_name] = self.removed.get(class_name, 0) + 1
        self.freed[class_name] = self.freed.get(class_name, 0) + freed

    def begin(self, class_name: str) -> None:
        if class_name not in self.ran:
            self.ran.append(class_name)
