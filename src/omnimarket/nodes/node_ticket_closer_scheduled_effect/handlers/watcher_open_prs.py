# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR watcher's state bound as the ticket closer's open-pull-request source (OMN-24301).

``WatcherOpenPullRequests`` answers ``open_for`` from the watcher state file, with the
schema and freshness the scope overlay names — the reading the closeout sweep already
trusts. A state that cannot be read as current is ``None``, never an empty set: the
candidate source reads its open pull requests as unread instead of deciding every ticket's
pull requests are merged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from omnimarket.models.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_rules import (
    open_prs,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
    read_watcher_state,
)


class WatcherOpenPullRequests:
    """The real backend open-pull-request source: the watcher state file, read as current."""

    def __init__(
        self,
        state_path: Path,
        scope: ModelCloseoutScopeOverlay,
        clock: Callable[[], datetime],
    ) -> None:
        self._state_path = state_path
        self._scope = scope
        self._clock = clock

    def open_for(self, ticket_ids: frozenset[str]) -> frozenset[str] | None:
        state, _ = read_watcher_state(self._state_path, self._clock(), self._scope)
        if state is None:
            return None
        opened = open_prs(state, self._scope)
        return frozenset(ticket_id for ticket_id in ticket_ids if ticket_id in opened)
