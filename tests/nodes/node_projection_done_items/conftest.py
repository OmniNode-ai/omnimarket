# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Shared fixtures for the done-items projection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_done_items.handlers.handler_done_items_writer import (
    DoneItemsProjectionWriter,
)
from omnimarket.nodes.node_projection_done_items.ports.done_items_store import (
    SqliteDoneItemsStore,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from tests.nodes.node_projection_done_items.test_done_items_projection import (
    Served,
    _topic_map,
)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Served:
    db = tmp_path / "done.sqlite"
    writer = DoneItemsProjectionWriter(store=SqliteDoneItemsStore(db))
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db}'\n"
    )
    monkeypatch.setenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", str(overlay))
    return writer, HandlerProjectionRead(topic_map=_topic_map())
