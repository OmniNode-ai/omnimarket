# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The batch PR state read the reconciler is injected with."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelPrNodeState,
)


@runtime_checkable
class ProtocolPrNodeStateReader(Protocol):
    """Read the state of up to 100 pull requests by node id in ONE call.

    A node id GitHub answers null for maps to None. A failed call raises; the
    reconciler then flips nothing from that batch and does not advance the
    cursor for its records.
    """

    def read_states(
        self, node_ids: Sequence[str]
    ) -> Mapping[str, ModelPrNodeState | None]: ...


__all__ = ["ProtocolPrNodeStateReader"]
