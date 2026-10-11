# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the host's PR watcher pointer out of every morning-node test."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    WATCHER_STATE_ENV,
)


@pytest.fixture(autouse=True)
def _no_host_watcher_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WATCHER_STATE_ENV, raising=False)
