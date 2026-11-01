# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CHG: the change source a node reads changes through.

Only the slice the release gate needs (OMN-21493): the history of one change. The
other reads of the protocol (open changes, head commits, tags, the release surface)
are added by the change-source task (T-B4). A source raises when the history cannot be
read; the caller treats that as ``blocked`` and never as an empty history.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.change_source.model_change_history import ModelChangeHistory


@runtime_checkable
class ProtocolChangeSource(Protocol):
    """Reads the ordered history of a change: label events and head pushes."""

    def change_history(
        self, repository: str, pull_request: int
    ) -> ModelChangeHistory: ...


__all__: list[str] = ["ProtocolChangeSource"]
